import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('publisher', Path(__file__).parents[1] / 'scripts/publisher.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PublishingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.vault = self.base / 'vault'; self.vault.mkdir()
        self.root = self.base / 'site'; (self.root / 'content').mkdir(parents=True)
        self.work = self.base / 'work'; self.work.mkdir()
        self.target = self.base / 'export'; self.target.mkdir()
        self.c = {'vault': self.vault, 'root': self.root, 'work': self.work, 'excluded_folders': ['敏感信息']}

    def tearDown(self): self.temp.cleanup()

    def note(self, name, body, meta=''):
        target = self.vault / name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(('---\n' + meta + '\n---\n') if meta else '', encoding='utf8')
        with target.open('a') as f: f.write(body)
        return target

    def export(self, notes):
        ex = p.Exporter(self.c, notes, self.target)
        posts = ex.export()
        text = (self.target / 'posts' / posts[0]['slug'] / 'index.md').read_text()
        return ex, posts, text

    def test_only_selected_note_and_public_metadata(self):
        self.note('a.md', '公开正文 %%不要公开%%', 'title: 公开标题\nprivate_field: secret-value')
        self.note('private.md', 'never-export-this')
        _, _, text = self.export(['a.md'])
        self.assertIn('公开正文', text)
        self.assertNotIn('不要公开', text)
        self.assertNotIn('private_field', text)
        self.assertNotIn('secret-value', text)
        self.assertEqual(len(list(self.target.rglob('*.md'))), 1)

    def test_images_vault_absolute_and_code_stays_literal(self):
        img = self.vault / 'images/一 张图.png'; img.parent.mkdir(); img.write_bytes(b'image')
        self.note('a.md', '![图](images/%E4%B8%80%20%E5%BC%A0%E5%9B%BE.png)\n\n`[[Not a link]]`\n\n```md\n![[not-real.png]]\n```')
        ex, _, text = self.export(['a.md'])
        self.assertIn('](assets/', text)
        self.assertIn('`[[Not a link]]`', text)
        self.assertIn('![[not-real.png]]', text)
        self.assertEqual(len(ex.assets), 1)

    def test_wikilink_between_selected_posts(self):
        self.note('a.md', '[[b|另一篇]]', 'slug: first')
        self.note('b.md', '第二篇', 'slug: second')
        _, _, text = self.export(['a.md', 'b.md'])
        self.assertIn('[另一篇](../second/)', text)

    def test_standard_link_between_selected_posts(self):
        self.note('a.md', '[另一篇](b.md)', 'slug: first')
        self.note('b.md', '第二篇', 'slug: second')
        _, _, text = self.export(['a.md', 'b.md'])
        self.assertIn('[另一篇](../second/)', text)

    def test_private_link_is_not_followed(self):
        self.note('a.md', '[[private]]')
        self.note('private.md', 'do-not-copy')
        with self.assertRaisesRegex(p.PublishError, '尚未公开'): self.export(['a.md'])
        self.assertFalse(any('do-not-copy' in f.read_text() for f in self.target.rglob('*.md')))

    def test_note_transclusion_fails_closed(self):
        self.note('a.md', '![[b]]'); self.note('b.md', 'private')
        with self.assertRaisesRegex(p.PublishError, '嵌入了另一篇'): self.export(['a.md', 'b.md'])

    def test_symlink_cannot_escape_vault(self):
        outside = self.base / 'private.png'; outside.write_bytes(b'private')
        (self.vault / 'image.png').symlink_to(outside)
        self.note('a.md', '![](image.png)')
        with self.assertRaisesRegex(p.PublishError, '以外'): self.export(['a.md'])

    def test_sensitive_folder_rejected(self):
        self.note('敏感信息/key.md', 'private')
        with self.assertRaisesRegex(p.PublishError, '不参与发布'): self.export(['敏感信息/key.md'])

    def test_queries_and_raw_html_are_not_silently_published(self):
        for content in ['```dataview\nLIST\n```', '<iframe src="something"></iframe>']:
            self.note('a.md', content)
            with self.assertRaises(p.PublishError): self.export(['a.md'])

    def test_secret_rejected_even_inside_code(self):
        self.note('a.md', '`sk-' + 'x' * 30 + '`')
        with self.assertRaisesRegex(p.PublishError, '密钥'): self.export(['a.md'])

    def test_reference_links_and_footnotes(self):
        (self.vault / 'photo.png').write_bytes(b'image')
        self.note('a.md', '![图片][image]\n\n[image]: photo.png\n\n一句话[^1]\n\n[^1]: 注释内容')
        _, _, text = self.export(['a.md'])
        self.assertIn('[image]: assets/', text)
        self.assertIn('[^1]: 注释内容', text)

    def test_title_slug_collision_rejected(self):
        self.note('a.md', 'a', 'slug: same'); self.note('b.md', 'b', 'slug: same')
        with self.assertRaisesRegex(p.PublishError, '相同'): self.export(['a.md', 'b.md'])

    def snapshot(self, draft=False):
        self.note('a.md', '正文', 'draft: ' + ('true' if draft else 'false'))
        sid = 'a' * 32
        folder = self.work / 'snapshots' / sid
        content = folder / 'content'; content.mkdir(parents=True)
        ex = p.Exporter(self.c, ['a.md'], content)
        posts = ex.export()
        manifest = dict(id=sid, source_hashes=ex.hashes, posts=posts, content_hash=p.tree_hash(content), site_hash=p.site_hash(self.c))
        p.write_json(folder / 'manifest.json', manifest)
        return sid, folder

    def test_unchanged_snapshot_validates(self):
        sid, _ = self.snapshot()
        self.assertEqual(p.validate_snapshot(self.c, sid)[1]['id'], sid)

    def test_changed_source_requires_new_preview(self):
        sid, _ = self.snapshot(); self.note('a.md', 'new private material')
        with self.assertRaisesRegex(p.PublishError, '发生了变化'): p.validate_snapshot(self.c, sid)

    def test_tampered_snapshot_rejected(self):
        sid, folder = self.snapshot()
        (folder / 'content/extra.md').write_text('unreviewed')
        with self.assertRaisesRegex(p.PublishError, '快照'): p.validate_snapshot(self.c, sid)

    def test_draft_can_preview_but_cannot_publish(self):
        sid, _ = self.snapshot(draft=True)
        with self.assertRaisesRegex(p.PublishError, 'draft'): p.validate_snapshot(self.c, sid)

    def test_changed_site_requires_new_preview(self):
        sid, _ = self.snapshot()
        (self.root / 'hugo.yaml').write_text('changed')
        with self.assertRaisesRegex(p.PublishError, '网站配置'): p.validate_snapshot(self.c, sid)


if __name__ == '__main__': unittest.main()
