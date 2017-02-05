import harfbuzz as hb
import qahirah as qh
import texlib.wrap as texwrap

ft = qh.get_ft_lib()


class Typesetter:

    def __init__(self, text, surface, text_width, page_width, page_height, top_margin, right_margin, debug=False):
        self.text = text
        self.text_width = text_width
        self.page_width = page_width
        self.page_height = page_height
        self.top_margin = top_margin
        self.right_margin = right_margin
        self.debug = debug

        ft_face = ft.find_face("Serif")
        ft_face.set_char_size(size=20, resolution=qh.base_dpi)
        self.font = hb.Font.ft_create(ft_face)
        self.buffer = hb.Buffer.create()

        cr = self.cr = qh.Context.create(surface)
        cr.font_face = qh.FontFace.create_for_ft_face(ft_face)
        cr.set_font_size(20)

        font_extents = self.font.get_h_extents()

        self.ascent = font_extents.ascender
        self.descent = -font_extents.descender
        self.line_gap = font_extents.line_gap

    def output(self):
        self._create_nodes()
        self._compute_breaks()
        self._draw_output()
        self.cr.show_page()

    def _create_nodes(self):
        nodes = self.nodes = texwrap.ObjectList()
        nodes.debug = self.debug

        space_adv = self.cr.text_extents(" ")[4]
        space_glue = texwrap.Glue(space_adv, space_adv / 2, space_adv / 2)

        buf = self.buffer
        font = self.font

        word = ""
        text = self.text + " " # XXX: hack
        for ch in text:
            if ch in " \u00A0":
                buf.reset()
                buf.add_str(word)
                buf.direction = hb.HARFBUZZ.DIRECTION_RTL
                buf.script = hb.HARFBUZZ.SCRIPT_ARABIC
                buf.language = hb.Language.from_string("ar")

                hb.shape(font, buf)
                glyphs, pos = buf.get_glyphs()

                nodes.append(texwrap.Box(pos.x, glyphs))
                if ch == "\u00A0":
                    nodes.append(texwrap.Penalty(0, texwrap.INFINITY))
                nodes.append(space_glue)
                word = ""
            else:
                word += ch

        nodes.pop() # XXX: hack, see above
        nodes.add_closing_penalty()

    def _compute_breaks(self):
        lengths = [self.text_width]
        self.breaks = self.nodes.compute_breakpoints(lengths)

    def _draw_output(self):
        self.cr.set_source_colour(qh.Colour.grey(0))

        lengths = [self.text_width]
        line_start = 0
        line = 0
        pos = qh.Vector(self.page_width - self.right_margin, self.top_margin)
        for breakpoint in self.breaks[1:]:
            pos.y += self.ascent
            pos.x = self.page_width - self.right_margin

            ratio = self.nodes.compute_adjustment_ratio(line_start, breakpoint, line, lengths)
            line += 1
            for i in range(line_start, breakpoint):
                box = self.nodes[i]
                if box.is_glue():
                    pos.x -= box.compute_width(ratio)
                elif box.is_box():
                    pos.x -= box.width
                    for glyph in box.character:
                        glyph.pos += pos
                    self.cr.show_glyphs(box.character)
                else:
                    pass
            line_start = breakpoint + 1

            pos.y += self.descent + self.line_gap

def main(text, text_width, debug, filename):
    top_margin = 10
    right_margin = 10
    page_width = text_width + right_margin * 2
    page_height = 1000
    surface = qh.PDFSurface.create(filename, (page_width, page_height))

    typesetter = Typesetter(text, surface, text_width, page_width, page_height, top_margin, right_margin, debug)
    typesetter.output()

if __name__ == "__main__":
    import sys
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])