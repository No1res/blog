# No1res · 随记

Hugo + PaperMod 博客，以及在 Obsidian 内选择文章、预览、发布的本地工具。

- 博客：https://no1res.github.io/blog/
- 交流区：https://github.com/No1res/blog/discussions
- RSS：https://no1res.github.io/blog/index.xml

## 日常使用

1. 在 Obsidian 点击左侧纸飞机图标，或按 `⌘P` 搜索「Hugo 博客发布：打开发布面板」。
2. 用「添加文章…」选择现有 Markdown 笔记，或用「新建草稿」开始写。
3. 点击「生成预览」。浏览器打开本机预览，文章和附件不会因此上传。
4. 检查文章、图片、链接和发布清单。草稿中的 `draft: true` 要改成 `draft: false` 才能正式发布。
5. 修改过正文或附件后，重新生成预览，然后点击「发布此预览」。
6. 等待面板显示「发布完成」。也可随时点「发布状态」或在 GitHub Actions 查看构建。

文章继续在 Obsidian 修改。更新文章时再次预览、发布即可。移出发布列表只停止更新，不会把已经上线的文章撤下。

每次预览只在本机监听 `127.0.0.1`。可以从命令面板执行「Hugo 博客发布：关闭本地预览」。

## 写作格式

建议每篇文章一个文件夹，正文叫 `index.md`，图片可以放在同目录或笔记库内其他位置。导出器只复制被引用的附件。

```yaml
---
title: 一个想与你讨论的问题
date: 2026-10-25
slug: a-question-worth-sharing
tags: [随笔]
description: 用一句话说明这篇文章在谈什么。
draft: true
---
```

`slug` 决定公开网址，只用小写英文、数字和短横线。文章发布后请保留它。没有填写时，发布工具会生成稳定的网址。

支持普通 Markdown、图片、PDF/音视频附件、标准链接、参考式链接，以及指向选中文章或已发布文章的 Obsidian 双链。普通图片会转换成随文章一起保存的资源。正文中的 Obsidian `%%注释%%` 和 HTML 注释会移除；代码块保持原样。

笔记嵌入、块引用链接、Dataview/Tasks 查询和原始 HTML 需要先整理成普通 Markdown。数学公式和 Mermaid 未配置专门的网页渲染，预览中确认显示后再发布。

链接到尚未公开的笔记时，工具会要求修正链接或一并选择目标文章。不会自动追踪链接并发布其他笔记。`敏感信息` 与隐藏文件夹不参加选择或导出。

## 发布边界

本机配置、笔记路径、发布清单与预览快照都留在本地。只有选定的正文、允许的文章属性与实际引用的附件进入博客仓库。原始 Obsidian 库不是网站的 Git 仓库。

「发布此预览」会核对原稿、附件、网站文件和预览快照。任何一项有变化，都需要重新预览。公开仓库内的正文及附件可被他人访问，因此仍应检查预览内容。工具识别一些常见密钥格式，但不能替代作者检查。

## 网站设置

- `hugo.yaml`：站名、简介、导航、RSS、交流入口。
- `content/about.md`：关于页面。
- `assets/css/extended/reading.css`：阅读排版。
- `themes/PaperMod/`：固定版本的上游主题，保留 MIT 许可；版本记录在 `THEME_VERSION.txt`。
- `.github/workflows/deploy.yml`：推送 main 后自动部署 GitHub Pages。

网站本身使用 Hugo 0.168.0。没有后台数据库或额外网站服务费用。

## 本地重装与迁移

需要 Git、Hugo、GitHub CLI 和 Python 3.11+。先执行 `gh auth login`，确认登录到有仓库写权限的账号。

在你选择的本地运行目录创建 Python 环境，然后用其中的 Python 安装 `requirements.txt`。接着运行：

```sh
python scripts/setup_local.py --vault "/你的/Obsidian笔记库" --work "/你的/博客本地运行目录"
```

这里的 `python` 应是刚创建的环境中的 Python。脚本会生成被 Git 忽略的 `.local.json`，安装本地插件并保留既有选择列表。然后在 Obsidian 第三方插件中启用「Hugo 博客发布」。更换仓库或地址时，给脚本加 `--repository 用户名/仓库名 --base-url https://你的网址/`，并同步修改 `hugo.yaml`。

换电脑时，除网站仓库和 Obsidian 笔记库外，请保留运行目录里的 `published.json`，它记录笔记与已发布文章的对应关系；不要上传这个本机清单。

## 出错时

- 原稿或附件已变化：重新预览。
- `draft: true`：改为 false，再预览。
- 找不到附件或存在重名：在 Obsidian 修正链接，使用明确路径。
- 推送失败：检查网络和 GitHub 登录，点「重试推送」；它不会重新导出未预览的正文。
- 远端有新改动：同步博客仓库后重新预览。
- 部署失败：查看仓库的 Actions。只有状态为 success 才表示网站更新完成。
- 仓库存在未提交改动：先处理网站代码或配置的改动，再发布文章。

## 验证

```sh
python -m unittest discover -s tests -v
hugo --destination /你的/临时构建目录
```

测试覆盖公开范围、私密双链、附件越界、代码与注释、草稿状态，以及预览后内容变化的阻断。
