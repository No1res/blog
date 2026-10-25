#!/usr/bin/env python3
"""Select locally, build an immutable preview, and publish exactly that snapshot."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import quote, unquote, urlsplit
import uuid
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
ASSETS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.svg', '.pdf', '.mp4', '.mp3', '.wav', '.ogg'}
SECRET = re.compile(r'(?:\bsk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{24,}|\bgh[pousr]_[A-Za-z0-9]{24,}|\bAKIA[A-Z0-9]{16}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


class PublishError(Exception):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else (default if default is not None else {})


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def run(args, cwd=ROOT, timeout=180):
    p = subprocess.run([str(x) for x in args], cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if p.returncode:
        raise PublishError((p.stderr or p.stdout or '操作失败').strip()[-4500:])
    return p.stdout.strip()


def config(path=ROOT / '.local.json'):
    if not path.exists():
        raise PublishError('尚未配置本机路径，请运行 scripts/setup_local.py。')
    c = read_json(path)
    c['root'] = Path(c.get('site_dir', ROOT)).resolve()
    c['vault'] = Path(c['vault_dir']).resolve()
    c['work'] = Path(c['work_dir']).resolve()
    c['work'].mkdir(parents=True, exist_ok=True)
    return c


@contextlib.contextmanager
def locked(c):
    with (c['work'] / 'operation.lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishError('正在进行另一项预览或发布，请稍后再试。')
        yield


def local_file(c, rel: str) -> Path:
    if not isinstance(rel, str) or Path(rel).is_absolute():
        raise PublishError('必须选择笔记库内的相对路径。')
    p = (c['vault'] / rel).resolve()
    if not p.is_relative_to(c['vault']):
        raise PublishError('文件超出了当前笔记库。')
    parts = p.relative_to(c['vault']).parts
    excluded = c.get('excluded_folders', ['敏感信息'])
    if any(x.startswith('.') for x in parts) or any(x in excluded for x in parts):
        raise PublishError('这份文件位于不参与发布的文件夹中。')
    if not p.is_file():
        raise PublishError(f'找不到文件：{rel}')
    return p


def frontmatter(text: str):
    text = text.lstrip('\ufeff')
    if not text.startswith('---\n') and not text.startswith('---\r\n'):
        return {}, text
    match = re.match(r'^---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        raise PublishError('文章属性没有正确结束，请检查开头的 ---。')
    try:
        data = yaml.safe_load(match[1]) or {}
    except yaml.YAMLError:
        raise PublishError('文章属性格式有误，请检查标题、日期和标签。')
    if not isinstance(data, dict):
        raise PublishError('文章属性必须是名称和值的对应关系。')
    return data, text[match.end():]


def mask_code(text):
    """Keep code verbatim while processing links/comments in prose only."""
    saved = []

    def stash(s):
        key = f'\x02CODE{len(saved)}\x03'
        saved.append(s)
        return key

    if '\x02' in text or '\x03' in text:
        raise PublishError('正文含有不支持的控制字符。')
    lines = text.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        m = re.match(r'^ {0,3}(`{3,}|~{3,})([^\n]*)', lines[i])
        if m:
            lang = m[2].strip().split(' ')[0].lower()
            if lang in {'dataview', 'dataviewjs', 'tasks'}:
                raise PublishError('文章中含有动态查询，请先把查询结果整理成普通文字或表格。')
            j = i + 1
            close = re.compile(r'^ {0,3}' + re.escape(m[1][0]) + '{' + str(len(m[1])) + r',}\s*$')
            while j < len(lines) and not close.match(lines[j]):
                j += 1
            j = min(j + 1, len(lines))
            out.append(stash(''.join(lines[i:j])) + '\n')
            i = j
        elif lines[i].startswith('    ') or lines[i].startswith('\t'):
            out.append(stash(lines[i]) + '\n')
            i += 1
        else:
            out.append(lines[i])
            i += 1
    protected = re.sub(r'(`+)(.+?)\1', lambda m: stash(m[0]), ''.join(out), flags=re.S)

    def restore(s):
        for n in range(len(saved) - 1, -1, -1):
            s = s.replace(f'\x02CODE{n}\x03', saved[n])
        return s
    return protected, restore


class Exporter:
    def __init__(self, c, entries, target: Path, previous=None):
        self.c, self.target = c, target
        self.previous = previous or {}
        self.posts, self.hashes, self.assets, self.warnings = {}, {}, {}, []
        self.files = None
        used = {v['slug']: k for k, v in self.previous.items()}
        for rel in entries:
            p = local_file(c, rel)
            if p.suffix.lower() != '.md':
                raise PublishError('请选择 Markdown 笔记。')
            text = p.read_text(encoding='utf-8')
            meta, body = frontmatter(text)
            if 'draft' in meta and not isinstance(meta['draft'], bool):
                raise PublishError('draft 必须是 true 或 false，请不要给它加引号。')
            self.hashes[rel] = digest(p)
            old = self.previous.get(rel, {})
            slug = str(meta.get('slug') or old.get('slug') or 'note-' + hashlib.sha256(rel.encode()).hexdigest()[:12])
            if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
                raise PublishError(f'文章 slug 请使用小写英文字母、数字和短横线：{p.stem}')
            if old.get('slug') and slug != old['slug']:
                raise PublishError('这篇文章已发布，请保留原来的 slug，以免原文链接失效。')
            if slug in used and used[slug] != rel:
                raise PublishError(f'两篇文章使用了相同的 slug：{slug}')
            if not old and (c['root'] / 'content/posts' / slug).exists():
                raise PublishError(f'网址 {slug} 已被另一篇文章使用。')
            used[slug] = rel
            if meta.get('draft') is True:
                self.warnings.append(f'“{meta.get("title", p.stem)}”标记为 draft；可预览，正式发布前需改成 false。')
            title = str(meta.get('title') or (p.parent.name if p.stem == 'index' else p.stem))
            self.posts[rel] = dict(slug=slug, title=title, meta=meta, body=body, path=p)

    def resolve(self, raw, source):
        path = unquote(raw).replace('\\ ', ' ')
        candidates = [source.parent / path, self.c['vault'] / path.lstrip('/')]
        if not Path(path).suffix:
            candidates += [p.with_suffix('.md') for p in list(candidates)]
        for p in candidates:
            if p.is_file():
                resolved = p.resolve()
                if not resolved.is_relative_to(self.c['vault']):
                    raise PublishError('文章引用了笔记库以外的文件。')
                return local_file(self.c, str(resolved.relative_to(self.c['vault'])))
        if '/' not in path:
            if self.files is None:
                self.files = [p for p in self.c['vault'].rglob('*') if p.is_file() and not any(x.startswith('.') for x in p.relative_to(self.c['vault']).parts)]
            matches = [p for p in self.files if p.name == path or p.name == path + '.md']
            if len(matches) == 1:
                return local_file(self.c, str(matches[0].relative_to(self.c['vault'])))
            if len(matches) > 1:
                raise PublishError(f'存在重名附件或笔记，请补全链接路径：{path}')
        raise PublishError(f'找不到文章引用的文件：{raw}')

    def destination(self, dest, rel, image=False):
        dest = dest.strip()
        parsed = urlsplit(dest)
        if parsed.scheme:
            if parsed.scheme.lower() in {'https', 'http', 'mailto', 'tel'}:
                return dest
            raise PublishError('文章包含本地应用链接或不支持的链接协议，请替换为公开网址。')
        if dest.startswith('//'):
            return 'https:' + dest
        if dest.startswith('#'):
            if dest.startswith('#^'):
                raise PublishError('块引用链接无法直接用于网页，请改为标题链接。')
            return dest
        raw, sep, anchor = dest.partition('#')
        if '?' in raw:
            raise PublishError('本地附件链接不能带查询参数。')
        target = self.resolve(raw, self.posts[rel]['path'])
        target_rel = str(target.relative_to(self.c['vault']))
        if target.suffix.lower() == '.md':
            if image:
                raise PublishError('文章嵌入了另一篇笔记。请把需要公开的段落直接整理进正文。')
            other = self.posts.get(target_rel) or self.previous.get(target_rel)
            if not other:
                raise PublishError('文章链接到了尚未公开的笔记。请一并选择该笔记，或把链接改成普通文字。')
            if anchor.startswith('^'):
                raise PublishError('块引用链接无法直接用于网页，请改为标题链接。')
            heading_id = ''.join(ch for ch in unquote(anchor).lower() if ch.isalnum() or ch in ' _-').replace(' ', '-')
            suffix = '#' + quote(heading_id, safe='-') if sep else ''
            return '../' + other['slug'] + '/' + suffix
        if target.suffix.lower() not in ASSETS:
            raise PublishError(f'暂不支持发布这种附件：{target.suffix}')
        h = digest(target)
        self.hashes[target_rel] = h
        name = h[:16] + target.suffix.lower()
        folder = self.target / 'posts' / self.posts[rel]['slug']
        (folder / 'assets').mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, folder / 'assets' / name)
        self.assets[target_rel] = name
        return 'assets/' + name + ('#' + anchor if sep else '')

    def rewrite(self, body, rel):
        text, restore = mask_code(body)
        if text.count('%%') % 2:
            raise PublishError('Obsidian 私密注释没有闭合，请检查 %% 标记。')
        text = re.sub(r'%%.*?%%', '', text, flags=re.S)
        text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
        if re.search(r'</?[A-Za-z][\w-]*(?:\s[^<>]*|/?)>', text):
            raise PublishError('公开稿包含原始 HTML，请先改为普通 Markdown，避免嵌入内容显示不一致。')

        def wiki(m):
            image = bool(m[1])
            inner = m[2]
            target, _, label = inner.partition('|')
            url = self.destination(target, rel, image)
            label = label or target.split('#')[0].rsplit('/', 1)[-1]
            if image and re.fullmatch(r'\d+(?:x\d+)?', label):
                label = '图片'
            label = label.replace('[', '\\[').replace(']', '\\]')
            return ('!' if image else '') + f'[{label}]({url})'

        # Rewrite standard links before wiki links, so emitted destinations aren't re-exported.
        out, i = [], 0
        while i < len(text):
            start = i
            image = text.startswith('![', i) and not text.startswith('![[', i)
            bracket = i + 1 if image else i
            if text[bracket:bracket + 1] != '[' or text[bracket:bracket + 2] == '[[' or (i and text[i - 1] == '\\'):
                out.append(text[i]); i += 1; continue
            j, depth = bracket + 1, 1
            while j < len(text) and depth:
                if text[j] == '\\': j += 2; continue
                if text[j] == '[': depth += 1
                if text[j] == ']': depth -= 1
                j += 1
            if depth or text[j:j + 1] != '(':
                out.append(text[i]); i += 1; continue
            k, depth, quoted = j + 1, 1, None
            while k < len(text) and depth:
                ch = text[k]
                if ch == '\\': k += 2; continue
                if quoted:
                    if ch == quoted: quoted = None
                elif ch in {'"', "'"} and (k == j + 1 or text[k - 1].isspace()): quoted = ch
                elif ch == '(': depth += 1
                elif ch == ')': depth -= 1
                k += 1
            if depth:
                raise PublishError('文章有未闭合的 Markdown 链接。')
            inside = text[j + 1:k - 1].strip()
            m = re.match(r'^(<[^>]*>|(?:\\.|[^\s])+)(.*)$', inside, re.S)
            if not m:
                out.append(text[start:k]); i = k; continue
            dest = m[1][1:-1] if m[1].startswith('<') else m[1]
            dest = re.sub(r'\\([() ])', r'\1', dest)
            url = self.destination(dest, rel, image)
            out.append(text[start:j + 1] + url + m[2] + ')')
            i = k
        text = ''.join(out)

        def reference(m):
            dest = m[2].strip('<>')
            return m[1] + self.destination(dest, rel) + m[3]
        text = re.sub(r'(?m)^( {0,3}\[(?!\^)[^\]\n]+\]:\s*)(<[^>\n]+>|\S+)([^\n]*)$', reference, text)
        text = re.sub(r'(!?)\[\[([^\]\n]+)\]\]', wiki, text)
        text = restore(text)
        if SECRET.search(text):
            raise PublishError('公开稿中发现疑似访问密钥，请移除后重新预览。')
        if re.search(r'(?:file://|obsidian://|/Users/[^\s/]+/)', text):
            raise PublishError('公开稿中含有本机文件地址，请换成公开链接或附件。')
        return text

    def export(self):
        results = []
        for rel, post in self.posts.items():
            body = self.rewrite(post['body'], rel)
            raw = post['meta']
            date = raw.get('date') or self.previous.get(rel, {}).get('date') or dt.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
            if isinstance(date, (dt.date, dt.datetime)):
                date = date.isoformat()
            tags = raw.get('tags', [])
            if isinstance(tags, str): tags = [tags]
            if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
                raise PublishError('标签需要是文字列表。')
            meta = dict(title=post['title'], date=str(date), slug=post['slug'], tags=tags,
                        description=str(raw.get('description') or raw.get('summary') or ''),
                        draft=False, lastmod=dt.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'))
            serialized = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
            if SECRET.search(serialized):
                raise PublishError('文章属性中发现疑似访问密钥。')
            folder = self.target / 'posts' / post['slug']
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'index.md').write_text('---\n' + serialized + '---\n\n' + body.strip() + '\n')
            results.append(dict(source=rel, slug=post['slug'], title=post['title'], date=str(date),
                                draft=raw.get('draft') is True, url=f'posts/{post["slug"]}/'))
        return results


def tree_hash(folder):
    h = hashlib.sha256()
    for p in sorted(folder.rglob('*')):
        if p.is_file():
            h.update(str(p.relative_to(folder)).encode()); h.update(p.read_bytes())
    return h.hexdigest()


def site_hash(c):
    h = hashlib.sha256()
    for name in ['hugo.yaml', 'layouts', 'assets', 'static', 'themes', 'content']:
        p = c['root'] / name
        h.update((digest(p) if p.is_file() else tree_hash(p) if p.is_dir() else '').encode())
    return h.hexdigest()


def build(c, content, destination, base_url):
    run([c['hugo'], '--source', c['root'], '--contentDir', content, '--destination', destination,
         '--baseURL', base_url, '--cacheDir', c['work'] / 'hugo-cache', '--cleanDestinationDir'], cwd=c['root'])


def preview(c, entries, serve=True):
    if not entries:
        raise PublishError('请先选择至少一篇文章。')
    entries = list(dict.fromkeys(entries))
    sid = uuid.uuid4().hex
    folder = c['work'] / 'snapshots' / sid
    content = folder / 'content'
    shutil.copytree(c['root'] / 'content', content)
    state = read_json(c['work'] / 'published.json')
    previous = state.get('posts', {})
    exporter = Exporter(c, entries, content, previous)
    # Remove assets from the prior version of selected posts before generating their new version.
    for post in exporter.posts.values():
        target = content / 'posts' / post['slug']
        if target.exists(): shutil.rmtree(target)
    posts = exporter.export()
    public = folder / 'public'
    port = start_server(c, public, sid) if serve else 1313
    try:
        build(c, content, public, f'http://127.0.0.1:{port}/')
        for post in posts:
            if not (public / post['url'] / 'index.html').exists():
                raise PublishError('Hugo 没有生成这篇文章，请检查日期是否晚于今天。')
        manifest = dict(id=sid, posts=posts, source_hashes=exporter.hashes,
                        content_hash=tree_hash(content), site_hash=site_hash(c),
                        warnings=exporter.warnings, attachments=len(exporter.assets))
        write_json(folder / 'manifest.json', manifest)
    except Exception:
        if serve: stop_server(c)
        raise
    return dict(ok=True, snapshot=sid, url=f'http://127.0.0.1:{port}/', posts=posts,
                attachments=len(exporter.assets), warnings=exporter.warnings)


def start_server(c, directory, sid):
    stop_server(c)
    directory.mkdir(parents=True, exist_ok=True)
    status = c['work'] / 'preview-server.json'
    if status.exists(): status.unlink()
    with (c['work'] / 'preview.log').open('a') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve', '--directory', str(directory),
                          '--status-file', str(status), '--snapshot', sid], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        if status.exists(): return read_json(status)['port']
        time.sleep(.05)
    raise PublishError('本地预览服务启动失败。')


def stop_server(c):
    p = c['work'] / 'preview-server.json'
    status = read_json(p)
    pid = status.get('pid')
    if pid:
        check = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], text=True, capture_output=True).stdout
        if 'publisher.py serve ' in check and str(c['work']) in check:
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
    if p.exists(): p.unlink()


def validate_snapshot(c, sid):
    if not re.fullmatch(r'[a-f0-9]{32}', sid or ''):
        raise PublishError('请先生成预览。')
    folder = c['work'] / 'snapshots' / sid
    m = read_json(folder / 'manifest.json')
    if not m or m.get('id') != sid:
        raise PublishError('预览已失效，请重新生成。')
    for rel, expected in m['source_hashes'].items():
        if digest(local_file(c, rel)) != expected:
            raise PublishError('预览之后原稿或附件发生了变化，请重新预览再发布。')
    if site_hash(c) != m['site_hash']:
        raise PublishError('预览之后网站配置或已发布内容有变化，请重新预览。')
    if tree_hash(folder / 'content') != m['content_hash']:
        raise PublishError('预览快照发生了变化，请重新预览。')
    if any(p['draft'] for p in m['posts']):
        raise PublishError('选中文章仍标记为 draft: true，请先改为 false，再预览并发布。')
    return folder, m


def publish(c, sid):
    folder, m = validate_snapshot(c, sid)
    root = c['root']
    if run(['git', 'branch', '--show-current'], root) != 'main':
        raise PublishError('请切换到博客的 main 分支后发布。')
    if run(['git', 'status', '--porcelain'], root):
        raise PublishError('博客源文件有尚未提交的改动，请先保存这些网站改动，再重新预览。')
    run(['git', 'fetch', 'origin', 'main'], root)
    if run(['git', 'rev-list', '--count', 'HEAD..origin/main'], root) != '0':
        raise PublishError('远端网站有新改动，请先同步博客仓库，再重新预览。')
    build(c, folder / 'content', folder / 'production', c['base_url'])
    paths = []
    for p in m['posts']:
        relative = 'content/posts/' + p['slug']
        dest = root / relative
        if dest.exists(): shutil.rmtree(dest)
        shutil.copytree(folder / relative, dest)
        paths.append(relative)
    run(['git', 'add', '--', *paths], root)
    changes = run(['git', 'diff', '--cached', '--name-only'], root)
    if changes:
        run(['git', 'commit', '-m', 'Publish selected blog posts'], root)
    sha = run(['git', 'rev-parse', 'HEAD'], root)
    # Preserve the mapping even if a network failure requires retrying the push.
    state = read_json(c['work'] / 'published.json', {'posts': {}})
    for p in m['posts']:
        state.setdefault('posts', {})[p['source']] = {k: p[k] for k in ['slug', 'title', 'date', 'url']}
    state['commit'] = sha
    state['snapshot'] = sid
    write_json(c['work'] / 'published.json', state)
    run(['git', 'push', 'origin', 'main'], root)
    return dict(ok=True, commit=sha, url=c['base_url'], actions_url=f'https://github.com/{c["repository"]}/actions',
                message='已提交到 GitHub，正在生成公开网站。')


def status(c):
    state = read_json(c['work'] / 'published.json')
    sha = state.get('commit') or run(['git', 'rev-parse', 'HEAD'], c['root'])
    data = json.loads(run([c['gh'], 'api', f'repos/{c["repository"]}/actions/runs?head_sha={sha}&per_page=10'], c['root']))
    runs = [r for r in data.get('workflow_runs', []) if r.get('path') == '.github/workflows/deploy.yml']
    latest = runs[0] if runs else {}
    return dict(ok=True, commit=sha, status=latest.get('status', 'queued'), conclusion=latest.get('conclusion'),
                run_url=latest.get('html_url'), url=c['base_url'])


def serve(directory, statefile, sid):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)
        def log_message(self, *_): pass
        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
    write_json(Path(statefile), dict(pid=os.getpid(), port=server.server_port, snapshot=sid))
    server.serve_forever()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['preview', 'publish', 'status', 'stop', 'serve', 'retry'])
    p.add_argument('--config', type=Path, default=ROOT / '.local.json')
    p.add_argument('--request', type=Path)
    p.add_argument('--snapshot')
    p.add_argument('--directory')
    p.add_argument('--status-file')
    args = p.parse_args()
    if args.command == 'serve':
        serve(args.directory, args.status_file, args.snapshot); return
    c = config(args.config)
    with locked(c):
        if args.command == 'preview':
            request = read_json(args.request)
            if str(Path(request['vault']).resolve()) != str(c['vault']):
                raise PublishError('当前打开的笔记库与博客配置不一致。')
            result = preview(c, request['notes'])
        elif args.command == 'publish': result = publish(c, args.snapshot)
        elif args.command == 'status': result = status(c)
        elif args.command == 'retry':
            pending = read_json(c['work'] / 'published.json').get('commit')
            if not pending or run(['git', 'rev-parse', 'HEAD'], c['root']) != pending:
                raise PublishError('没有与当前预览对应的待推送版本，请重新预览并发布。')
            run(['git', 'push', 'origin', 'main'], c['root'])
            result = dict(ok=True, message='已重试推送，请查看发布状态。')
        else:
            stop_server(c); result = dict(ok=True)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (PublishError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(json.dumps(dict(ok=False, error=str(e)), ensure_ascii=False))
        sys.exit(1)
