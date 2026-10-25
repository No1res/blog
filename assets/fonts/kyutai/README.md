# 《球体》阅读字体

这两份 WOFF2 是 Noto Serif CJK SC 2.003 的 Regular / SemiBold 字符子集，
以「Kyutai Reading Serif」作为派生字体名称。

- 原始字体：<https://github.com/notofonts/noto-cjk/tree/main/Serif>
- 原始版权：© 2017–2024 Adobe (http://www.adobe.com/).
- 许可：SIL Open Font License 1.1，见同目录 `OFL.txt`。
- 用途：仅《球体》文章加载，包含当前正文、日文歌词、导航及常用标点。
- 字体随网站提供，不依赖外部字体 CDN；缺少的新增字符会使用系统宋体回退。

文章大幅增补后，可用 `scripts/build_kyutai_fonts.py` 重新生成子集。
该可选维护脚本需 `fonttools[woff]`，输入目录应含上述两个原版 OTF 文件。
普通 Hugo 构建和 Obsidian 预览、发布不需要字体处理依赖。
