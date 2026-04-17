#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <ft2build.h>
#include FT_FREETYPE_H

int main(int argc, char **argv) {
    if (argc < 2) return 1;
    FT_Library library;
    FT_Face face;
    if (FT_Init_FreeType(&library)) return 1;
    if (FT_New_Face(library, argv[1], 0, &face) == 0) {
        FT_Set_Pixel_Sizes(face, 0, 16);
        /* 遍历前几个字形 */
        for (unsigned long i = 0; i < face->num_glyphs && i < 32; i++) {
            FT_Load_Glyph(face, i, FT_LOAD_DEFAULT);
            FT_Render_Glyph(face->glyph, FT_RENDER_MODE_NORMAL);
        }
        FT_Done_Face(face);
    }
    FT_Done_FreeType(library);
    return 0;
}
