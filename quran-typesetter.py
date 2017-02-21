import logging
import math

import harfbuzz as hb
import qahirah as qh
import texlib.wrap as texwrap

ft = qh.get_ft_lib()

logging.basicConfig(format="%(asctime)s - %(message)s")
logger = logging.getLogger("typesetter")
logger.setLevel(logging.INFO)


class Settings:
    """Class holding document wide settings."""

    def __init__(self):
        # The defaults here roughly match “the 12-lines Mushaf”.
        self.body_font        = "Amiri Quran"
        self.body_font_size   = 11.5
        self.lines_per_page   = 12
        self.leading          = 29  # ~0.4in
        self.text_widths      = [205] # ~2.84in
        self.page_width       = 396 # 5.5in
        self.page_height      = 540 # 7.5in
        # From top of page to first baseline.
        self.top_margin       = 105 # ~1.46
        self.outer_margin     = 100 # ~1.4in
        self.page_number_ypos = 460 # ~6.4in

    def get_text_width(self, line):
        if line >= len(self.text_widths):
            line = -1
        return self.text_widths[line]

    def get_page_number_pos(self, page, width):
        pos = qh.Vector(0, self.page_number_ypos)

        # Center the number relative to the text box.
        line = self.lines_per_page - 1
        text_width = self.get_text_width(line)
        pos.x = self.get_text_start_pos(page, line)
        pos.x -= text_width / 2

        # Center the box around the position
        pos.x -= width / 2

        return pos

    def get_text_start_pos(self, page, line):
        if page.number % 2 == 0:
            return self.page_width - self.outer_margin
        else:
            return self.outer_margin + self.get_text_width(line)

    def get_side_mark_pos(self, page, line, width):
        x = (self.outer_margin / 2) - (width / 2)
        if page.number % 2 == 0:
            x += self.get_text_start_pos(page, line)
        return x


class State:
    """Class holding document wide state."""

    def __init__(self):
        self.quarter = 1


class Document:
    """Class representing the main document and holding document-wide settings
       and state."""

    def __init__(self, chapters, filename):
        logger.debug("Initializing the document: %s", filename)

        self.settings = settings = Settings()
        self.state = State()
        self.shaper = Shaper(self)

        surface = qh.PDFSurface.create(filename, (settings.page_width,
                                                  settings.page_height))
        # Create a new FreeType face for Cairo, as sometimes Cairo mangles the
        # char size, breaking HarfBuzz positions when it uses the same face.
        ft_face = ft.find_face(settings.body_font)
        cr = self.cr = qh.Context.create(surface)
        cr.set_font_face(qh.FontFace.create_for_ft_face(ft_face))
        cr.set_font_size(settings.body_font_size)
        cr.set_source_colour(qh.Colour.grey(0))

        self.chapters = chapters

    def save(self):
        lines = self._create_lines()
        pages = self._create_pages(lines)

        logger.info("Drawing pages…")
        for page in pages:
            page.draw(self.cr, self.shaper, self.settings, self.state)

    def _create_lines(self):
        """Processes each chapter and creates lines for the whole document."""

        logger.info("Breaking text into lines…")

        lines = LineList()
        for chapter in self.chapters:
            lines.extend(self._process_chapter(chapter))

        return lines

    def _create_pages(self, lines):
        """Breaks the lines into pages"""

        logger.info("Breaking lines into pages…")

        pages = [Page([], 1)]
        lengths = [self.settings.leading * self.settings.lines_per_page]
        breaks = lines.compute_breakpoints(lengths)
        assert breaks[-1] == len(lines) - 1

        start = 0
        for i, breakpoint in enumerate(breaks[1:]):
            ratio = lines.compute_adjustment_ratio(start, breakpoint, i,
                                                   lengths)

            page = Page([], len(pages) + 1)
            for j in range(start, breakpoint):
                line = lines[j]
                if line.is_glue():
                    line.height = line.compute_width(ratio)
                page.lines.append(line)

            while not page.lines[-1].is_box():
                page.lines.pop()

            pages.append(page)
            start = breakpoint + 1

        return pages

    def _create_heading(self, chapter):
        lines = []
        for text in chapter.get_heading_text():
            boxes = self.shaper.shape_paragraph(text)
            lines.append(Line(self.settings.leading, boxes))

        return Heading(self.settings.leading, lines)

    def _process_chapter(self, chapter):
        """Shapes the text and breaks it into lines."""

        lengths = self.settings.text_widths
        nodes = self.shaper.shape_paragraph(chapter.text)
        breaks = nodes.compute_breakpoints(lengths, tolerance=20)
        assert breaks[-1] == len(nodes) - 1

        lines = [self._create_heading(chapter)]
        if chapter.opening:
            box = self.shaper.shape_word("\uFDFD")
            lines.append(Line(self.settings.leading, [box]))

        start = 0
        for i, breakpoint in enumerate(breaks[1:]):
            ratio = nodes.compute_adjustment_ratio(start, breakpoint, i,
                                                   lengths)

            boxes = []
            for j in range(start, breakpoint):
                box = nodes[j]
                if box.is_glue():
                    box.width = box.compute_width(ratio)
                boxes.append(box)

            lines.append(Line(self.settings.leading, boxes))
            lines.append(LineGlue())

            start = breakpoint + 1

        # Allow stretching the glue between chapters.
        lines[-1].stretch = self.settings.leading

        return lines


class Chapter:
    """Class holding input text and metadata for a chapter."""

    def __init__(self, text, number, name, place, opening, verses):
        self.text = text
        self.number = number
        self.name = name
        self.place = place
        self.opening = opening
        self.verses = verses

    def get_heading_text(self):
        text = []
        number = format_number(self.number)
        verses = format_number(self.verses)
        text.append("(%s) سورة %s %s" % (number, self.name, self.place))
        text.append("و آياتها %s" % verses)

        return text


class Shaper:
    """Class for turning text into boxes and glue."""

    # Cache for shaped words.
    word_cache = {}

    def __init__(self, doc):
        self.doc = doc
        ft_face = ft.find_face(doc.settings.body_font)
        ft_face.set_char_size(size=doc.settings.body_font_size,
                              resolution=qh.base_dpi)
        self.font = hb.Font.ft_create(ft_face)
        self.buffer = hb.Buffer.create()

    def shape_word(self, word):
        """
        Shapes a single word and returns the corresponding box. To speed things
        a bit, we cache the shaped words. We assume all our text is in Arabic
        script and language. The direction is almost always right-to-left,
        (we are cheating a bit to avoid doing proper bidirectional text as
        it is largely superfluous for us here).
        """

        assert word

        if word not in self.word_cache:
            self.buffer.clear_contents()
            self.buffer.add_str(word)
            # Everything is RTL except aya numbers and other digits-only words.
            if word[0] in ("\u06DD", "(") or word.isdigit():
                self.buffer.direction = hb.HARFBUZZ.DIRECTION_LTR
            else:
                self.buffer.direction = hb.HARFBUZZ.DIRECTION_RTL
            self.buffer.script = hb.HARFBUZZ.SCRIPT_ARABIC
            self.buffer.language = hb.Language.from_string("ar")

            hb.shape(self.font, self.buffer)

            glyphs, pos = self.buffer.get_glyphs()
            box = Box(pos.x, glyphs)

            # Flag boxes with “quarter” symbol, as it needs some special
            # handling later.
            if word.startswith("\u06DE"):
                box.quarter = True

            self.word_cache[word] = box

        return self.word_cache[word]

    def shape_paragraph(self, text):
        """
        Converts the text to a list of boxes and glues that the line breaker
        will work on. We basically split text into words and shape each word
        separately then put it into a box. We don’t try to preserve the
        context when shaping the words, as we know that our font does not
        do anything special around spaces, which in turn allows us to cache
        the shaped words.
        """
        nodes = texwrap.ObjectList()

        # Get the natural space width
        space = self.shape_word(" ").width

        # Split the text into words, treating space, newline and no-break space
        # as word separators.
        word = ""
        for i, ch in enumerate(text.strip()):
            if ch in (" ", "\n", "\u00A0"):
                # Drop quarter glyph at start of chapter but keep the mark.
                if ch == "\u00A0" and i == 1 and text[0] == "\u06DE":
                    box = Box(0, [])
                    box.quarter = True
                    nodes.append(box)
                    word = ""
                    continue

                nodes.append(self.shape_word(word))

                # Prohibit line breaking at no-break space.
                if ch == "\u00A0":
                    nodes.append(Penalty(0, texwrap.INFINITY))

                nodes.append(Glue(space, space / 2, space / 2))
                word = ""
            else:
                word += ch
        nodes.append(self.shape_word(word)) # last word

        nodes.add_closing_penalty()

        return nodes


def format_number(number):
    """Format number to Arabic-Indic digits."""

    number = int(number)
    return "".join([chr(ord(c) + 0x0630) for c in str(number)])


class Page:
    """Class representing a page of text."""

    def __init__(self, lines, number):
        self.lines = lines
        self.number = number

    def draw(self, cr, shaper, settings, state):
        logger.debug("Drawing page %d…", self.number)

        self.cr = cr

        if not self.lines:
            logger.debug("Leaving empty page blank")
            cr.show_page()
            return

        lines = self.lines
        pos = qh.Vector(0, settings.top_margin)
        for i, line in enumerate(lines):
            pos.x = settings.get_text_start_pos(self, i)
            text_width = settings.get_text_width(i)
            line.draw(cr, pos, text_width)
            if line.has_quarter():
                self._show_quarter(i, pos.y, state.quarter, shaper, settings)
                state.quarter += 1
            pos.y += line.height

        # Show page number.
        box = shaper.shape_word(format_number(self.number))
        pos = settings.get_page_number_pos(self, box.width)
        box.draw(cr, pos)

        # Draw page decorations.
        o = 8
        x = settings.get_text_start_pos(self, 0) + o
        y = settings.top_margin - settings.leading / 2 - o
        w = settings.get_text_width(0) + o * 2
        h = settings.leading * settings.lines_per_page + o

        cr.save()
        rect = qh.Rect(x - w, y, w, h)
        cr.rectangle(rect)
        cr.set_line_width(1)
        cr.stroke()
        cr.rectangle(rect.inset((-5, -5)))
        cr.set_line_width(3)
        cr.stroke()
        cr.restore()

        cr.show_page()

    def _show_quarter(self, line, y, quarter, shaper, settings):
        """
        Draw the quarter, group and part text on the margin. A group is 4
        quarters, a part is 2 groups.
        """

        boxes = []
        num = quarter % 4
        if num:
            # A quarter.
            words = ("ربع", "نصف", "ثلاثة أرباع")
            boxes.append(shaper.shape_word(words[num - 1]))
            boxes.append(shaper.shape_word("الحزب"))
        else:
            # A group…
            group = format_number((quarter / 4) + 1)
            if quarter % 8:
                # … without a part.
                boxes.append(shaper.shape_word("حزب"))
                boxes.append(shaper.shape_word(group))
            else:
                # … with a part.
                part = format_number((quarter / 8) + 1)
                # XXX: [::-1] is a hack to get the numbers LTR
                boxes.append(shaper.shape_word("حزب %s" % group[::-1]))
                boxes.append(shaper.shape_word("جزء %s" % part[::-1]))

        # We want the text to be smaller than the body size…
        scale = .8
        # … and the leading to be tighter.
        leading = settings.body_font_size

        w = max([box.width for box in boxes])
        x = settings.get_side_mark_pos(self, line, w)
        # Center the boxes vertically around the line.
        # XXX: should use the box height / 2
        y -= leading / 2
        for box in boxes:
            # Center the box horizontally relative to the others
            offset = (w - box.width) * scale / 2

            self.cr.save()
            self.cr.translate((x + offset, y))
            self.cr.scale((scale, scale))
            self.cr.show_glyphs(box.glyphs)
            self.cr.restore()

            y += leading


class LineList(texwrap.ObjectList):

    def compute_breakpoints(self, line_lengths):
        # Copied from compute_breakpoints() since compute_adjustment_ratio()
        # needs them.
        self.sum_width = {}
        self.sum_shrink = {}
        self.sum_stretch = {}
        width_sum = shrink_sum = stretch_sum = 0
        for i, node in enumerate(self):
            self.sum_width[i] = width_sum
            self.sum_shrink[i] = shrink_sum
            self.sum_stretch[i] = stretch_sum

            width_sum += node.height
            shrink_sum += node.shrink
            stretch_sum += node.stretch

        # Calculate line breaks.
        # XXX: This seems rather hackish, clean it up!
        breaks = [0]
        height = 0
        last = 0
        i = 0
        while i < len(self):
            line = len(breaks)
            length = line_lengths[line if line < len(line_lengths) else -1]

            node = self[i]
            if node.is_box() or node.is_glue():
                height += node.height

            if not node.is_box():
                if height > length:
                    breaks.append(last)
                    height = 0
                    i = last
                elif height == length:
                    breaks.append(i)
                    height = 0
                else:
                    last = i
            i += 1

        if breaks[-1] != len(self) - 1:
            breaks.append(len(self) - 1)

        # Check that we are not overflowing the page, i.e. we don’t have more
        # lines per page (plus intervening glue) than we should.
        # XXX: 12 should be settings.lines_per_page
        last = 0
        for i in breaks[1:]:
            assert i - last <= 12 * 2, (i, i - last)
            last = i

        return breaks


class Glue(texwrap.Glue):
    """Wrapper around texwrap.Glue to hold our common API."""

    def draw(self, cr, pos, text_width=0):
        pass

    def has_quarter(self):
        return False


class Penalty(texwrap.Penalty):
    """Wrapper around texwrap.Penalty to hold our common API."""

    def draw(self, cr, pos, text_width=0):
        pass

    def has_quarter(self):
        return False


class Box(texwrap.Box):
    """Class representing a word."""

    def __init__(self, width, glyphs):
        super().__init__(width)
        self.glyphs = glyphs
        self.quarter = False

    def has_quarter(self):
        return self.quarter

    def draw(self, cr, pos, text_width=0):
        cr.save()
        cr.translate(pos)
        cr.show_glyphs(self.glyphs)
        cr.restore()


class LineGlue(Glue):
    def __init__(self, height=0, stretch=0, shrink=0):
        super().__init__(height, stretch, shrink)
        self.height = height


class Line(texwrap.Box):
    """Class representing a line of text."""

    def __init__(self, height, boxes):
        super().__init__(height)
        self.height = height
        self.boxes = boxes

    def has_quarter(self):
        return any([box.has_quarter() for box in self.boxes if box.is_box()])

    def draw(self, cr, pos, text_width):
        self.strip()
        width = sum([box.width for box in self.boxes])
        # Center lines not equal to text width.
        if not math.isclose(width, text_width):
            pos.x -= (text_width - width) / 2

        for box in self.boxes:
            # We start drawing from the right edge of the text block,
            # and move to the left, thus the subtraction instead of
            # addition below.
            pos.x -= box.width
            box.draw(cr, pos)

    def strip(self):
        while not self.boxes[-1].is_box():
            self.boxes.pop()


class Heading(Line):
    """Class representing a chapter heading."""

    def __init__(self, leading, lines):
        super().__init__(leading * 1.8, lines)
        self.leading = leading

    def draw(self, cr, pos, width):
        offset = self.leading / 2
        height = self.height - offset

        linepos = qh.Vector(pos.x, pos.y)
        for line in self.boxes:
            line.draw(cr, linepos, width)
            linepos.x = pos.x
            linepos.y += line.height - offset / 1.2

        cr.save()
        cr.set_line_width(.5)
        cr.move_to((pos.x, pos.y - offset))
        cr.rectangle(qh.Rect(pos.x - width, pos.y - offset, width, height))
        cr.stroke()
        cr.restore()


def main(chapters, filename):
    document = Document(chapters, filename)
    document.save()

if __name__ == "__main__":
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(description="Quran Typesetter.")
    parser.add_argument("datadir", metavar="DATADIR",
            help="Directory containing input files to process")
    parser.add_argument("outfile", metavar="OUTFILE",
            help="Output file")
    parser.add_argument("--chapters", "-c", metavar="N", nargs="*", type=int,
            choices=range(1, 115), default=range(1, 115),
            help="Which chapters to process (Default: all)")
    parser.add_argument("--quite", "-q", action="store_true",
            help="Don’t print normal messages")
    parser.add_argument("--verbose", "-v", action="store_true",
            help="Print verbose messages")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)
    if args.quite:
        logger.setLevel(logging.ERROR)

    path = os.path.join(args.datadir, "meta.txt")
    if os.path.isfile(path):
        with open(path, "r") as textfile:
            metadata = {}
            lines = [l.strip().split("\t") for l in textfile.readlines()]
            for num, line in enumerate(lines):
                num += 1
                metadata[num] = [line[0], line[1], True]
                if len(line) >= 3:
                    metadata[num][2] = int(line[2])
    else:
        logger.error("File not found: %s", path)
        sys.exit(1)

    chapters = []
    for i in args.chapters:
        path = os.path.join(args.datadir, "%03d.txt" % i)
        if os.path.isfile(path):
            with open(path, "r") as textfile:
                lines = textfile.readlines()
                chapter = Chapter("".join(lines), i, *metadata[i], len(lines))
                chapters.append(chapter)
        else:
            logger.error("File not found: %s", path)
            sys.exit(1)

    main(chapters, args.outfile)
