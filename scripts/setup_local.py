#!/usr/bin/env python3
"""Install the local Obsidian integration without changing existing notes."""
import argparse
import json
from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--vault', type=Path, required=True)
p.add_argument('--work', type=Path, required=True)
p.add_argument('--repository', default='No1res/blog')
p.add_argument('--base-url', default='https://no1res.github.io/blog/')
args = p.parse_args()
vault, work = args.vault.resolve(), args.work.resolve()
if not (vault / '.obsidian').is_dir():
    p.error('这个路径不是已经配置好的 Obsidian 笔记库。')
for tool in ['hugo', 'gh', 'git']:
    if not shutil.which(tool): p.error('请先安装 ' + tool)
work.mkdir(parents=True, exist_ok=True)
config = dict(vault_dir=str(vault), work_dir=str(work), site_dir=str(root), repository=args.repository,
              base_url=args.base_url, hugo=shutil.which('hugo'), gh=shutil.which('gh'), excluded_folders=['敏感信息'])
(root / '.local.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
plugin = vault / '.obsidian/plugins/hugo-publisher-local'
plugin.mkdir(parents=True, exist_ok=True)
for name in ['main.js', 'manifest.json', 'styles.css']:
    shutil.copy2(root / 'obsidian-plugin' / name, plugin / name)
data_path = plugin / 'data.json'
data = json.loads(data_path.read_text()) if data_path.exists() else {'selectedNotes': [], 'excludedFolders': ['敏感信息']}
data.update(siteDir=str(root), pythonPath=sys.executable)
data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
print('已安装本地插件；在 Obsidian 的第三方插件设置中启用「Hugo 博客发布」。')
