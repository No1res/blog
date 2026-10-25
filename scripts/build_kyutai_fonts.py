"""Optional font maintenance: pip install 'fonttools[woff]' before running.

Usage: python scripts/build_kyutai_fonts.py /path/to/NotoSerifCJKsc-fonts
Normal Hugo builds and Obsidian publishing use the checked-in WOFF2 files.
"""

import argparse
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("font_directory", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    text = (root / "content/posts/daichi-miura-kyutai-story/index.md").read_text()
    text += (root / "hugo.yaml").read_text()
    for path in (root / "layouts").rglob("*.html"):
        text += path.read_text()
    text += "目录分钟字阅读上一篇下一篇返回顶部日文原文中文意译展开收起交流"
    unicodes = set(map(ord, text))
    for start, end in [(0x20, 0xFF), (0x2000, 0x206F), (0x3000, 0x30FF), (0xFF00, 0xFFEF)]:
        unicodes.update(range(start, end + 1))
    output = root / "assets/fonts/kyutai"
    output.mkdir(parents=True, exist_ok=True)
    for weight in ["Regular", "SemiBold"]:
        font = TTFont(args.font_directory / f"NotoSerifCJKsc-{weight}.otf", recalcTimestamp=False)
        options = subset.Options()
        options.layout_features = ["*"]
        options.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14, 16, 17]
        options.name_languages = ["*"]
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=unicodes)
        subsetter.subset(font)
        # Give this derivative its own family name; preserve copyright/license.
        family = "Kyutai Reading Serif"
        names = {1: family, 2: weight, 3: f"{family} {weight} 2.003 subset",
                 4: f"{family} {weight}", 6: f"KyutaiReadingSerif-{weight}",
                 16: family, 17: weight}
        for record in font["name"].names:
            if record.nameID in names:
                record.string = names[record.nameID].encode(record.getEncoding())
        cff = font["CFF "].cff
        cff.fontNames = [names[6]]
        cff.topDictIndex[0].FamilyName = family
        cff.topDictIndex[0].FullName = names[4]
        font.flavor = "woff2"
        path = output / f"kyutai-serif-{weight.lower()}.woff2"
        font.save(path)
        print(f"{path.name}: {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
