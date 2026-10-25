import importlib.util
import io
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import unittest
from PIL import Image, ImageDraw

spec = importlib.util.spec_from_file_location('publisher', Path(__file__).parents[1] / 'scripts/publisher.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class ImageCompressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.vault = self.base / 'vault'; self.vault.mkdir()
        self.root = self.base / 'site'; (self.root / 'content').mkdir(parents=True)
        self.work = self.base / 'work'; self.work.mkdir()
        self.target = self.work / 'snapshots' / ('b' * 32) / 'content'
        self.target.mkdir(parents=True)
        self.c = dict(vault=self.vault, root=self.root, work=self.work)

    def tearDown(self):
        self.temp.cleanup()

    def export(self, body, second_body=None):
        names = []
        for slug, content in [('first', body), ('second', second_body)]:
            if content is not None:
                name = slug + '.md'
                (self.vault / name).write_text(f'---\nslug: {slug}\ndraft: false\n---\n{content}')
                names.append(name)
        ex = p.Exporter(self.c, names, self.target)
        posts = ex.export()
        return ex, posts

    def exported(self, ex, source, slug='first'):
        return self.target / 'posts' / slug / 'assets' / ex.assets[source]

    def test_photo_resizes_only_export_and_keeps_original_hash(self):
        source = self.vault / 'photo.jpg'
        Image.new('RGB', (3000, 1500), (70, 140, 210)).save(source, quality=98)
        before, mtime = source.read_bytes(), source.stat().st_mtime_ns
        ex, _ = self.export('![photo](photo.jpg)')
        result = self.exported(ex, 'photo.jpg')
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(source.stat().st_mtime_ns, mtime)
        self.assertEqual(ex.hashes['photo.jpg'], p.digest(source))
        self.assertEqual(result.suffix, '.webp')
        self.assertLess(result.stat().st_size, len(before))
        with Image.open(result) as im:
            self.assertEqual(im.size, (2000, 1000))
        summary = ex.image_summary()
        self.assertEqual((summary['count'], summary['optimized'], summary['resized']), (1, 1, 1))
        self.assertEqual(summary['saved_bytes'], len(before) - result.stat().st_size)

    def test_png_text_and_transparency_are_lossless_without_upscaling(self):
        img = Image.new('RGBA', (600, 240), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rectangle((20, 20, 580, 220), fill=(255, 255, 255, 255))
        draw.text((35, 35), 'A diagram: preserve sharp edges and labels', fill=(0, 0, 0, 255))
        draw.rectangle((35, 80, 150, 160), fill=(50, 120, 220, 128))
        img.save(self.vault / 'diagram.png', compress_level=0)
        ex, _ = self.export('![[diagram.png]]')
        with Image.open(self.exported(ex, 'diagram.png')) as out:
            self.assertEqual(out.size, img.size)
            self.assertEqual(out.convert('RGBA').tobytes(), img.tobytes())
        self.assertTrue(ex.images['diagram.png']['optimized'])
        self.assertFalse(ex.images['diagram.png']['resized'])

    def test_exif_rotation_applied_to_compressed_copy(self):
        img = Image.new('RGB', (120, 80), 'red')
        exif = Image.Exif(); exif[274] = 6; exif[270] = 'private camera metadata'
        img.save(self.vault / 'portrait.jpg', quality=98, exif=exif)
        ex, _ = self.export('![portrait](portrait.jpg)')
        with Image.open(self.exported(ex, 'portrait.jpg')) as out:
            self.assertEqual(out.size, (80, 120))
            self.assertFalse(out.getexif())
        with Image.open(self.vault / 'portrait.jpg') as original:
            self.assertEqual(original.getexif()[274], 6)

    def test_animated_images_keep_all_frames_and_exact_bytes(self):
        frames = [Image.new('RGB', (64, 32), color) for color in ('red', 'blue')]
        for ext in ('gif', 'png', 'webp'):
            with self.subTest(ext=ext):
                source = self.vault / ('animation.' + ext)
                frames[0].save(source, save_all=True, append_images=frames[1:], duration=[100, 250], loop=2)
                ex, _ = self.export(f'![animation]({source.name})')
                self.assertEqual(self.exported(ex, source.name).read_bytes(), source.read_bytes())
                self.assertEqual(ex.image_summary()['animated'], 1)
                self.assertEqual(ex.image_summary()['saved_bytes'], 0)

    def test_already_compressed_webp_not_reencoded(self):
        source = self.vault / 'small.webp'
        Image.new('RGB', (150, 80), 'red').save(source, quality=80)
        ex, _ = self.export('![small](small.webp)')
        self.assertEqual(self.exported(ex, source.name).read_bytes(), source.read_bytes())
        self.assertFalse(ex.images[source.name]['optimized'])

    def test_larger_candidate_is_not_used(self):
        # A JPEG already saved at very low quality should not grow on export.
        img = Image.frombytes('RGB', (96, 96), random.Random(7).randbytes(96 * 96 * 3))
        original = io.BytesIO(); img.save(original, format='JPEG', quality=5, optimize=True)
        data = original.getvalue()
        result, suffix, stats = p.compress_image(data, '.jpg')
        self.assertEqual(result, data)
        self.assertEqual(suffix, '.jpg')
        self.assertFalse(stats['optimized'])

    def test_shared_image_is_counted_once_and_available_in_both_posts(self):
        Image.new('RGB', (80, 50), 'blue').save(self.vault / 'shared.png', compress_level=0)
        ex, _ = self.export('![one](shared.png)\n![[shared.png]]', '![two](shared.png)')
        self.assertEqual(ex.image_summary()['count'], 1)
        self.assertEqual(self.exported(ex, 'shared.png').read_bytes(), self.exported(ex, 'shared.png', 'second').read_bytes())
        self.assertEqual(len(list((self.target / 'posts/first/assets').iterdir())), 1)

    def test_svg_and_downloads_copied_unchanged_and_external_images_not_downloaded(self):
        for name, data in [('plot.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>'), ('paper.pdf', b'%PDF-test')]:
            (self.vault / name).write_bytes(data)
        ex, _ = self.export('![plot](plot.svg)\n[paper](paper.pdf)\n![remote](https://example.com/photo.jpg)')
        self.assertEqual(ex.image_summary()['count'], 0)
        self.assertEqual(len(ex.assets), 2)
        for name in ex.assets:
            self.assertEqual(self.exported(ex, name).read_bytes(), (self.vault / name).read_bytes())

    def test_corrupt_image_fails_preview_with_filename(self):
        (self.vault / 'broken.png').write_bytes(b'not an image')
        with self.assertRaisesRegex(p.PublishError, 'broken.png.*无法读取'):
            self.export('![](broken.png)')

    def snapshot(self):
        Image.new('RGB', (300, 200), 'red').save(self.vault / 'photo.jpg', quality=98)
        ex, posts = self.export('![photo](photo.jpg)')
        sid = self.target.parent.name
        p.write_json(self.target.parent / 'manifest.json', dict(id=sid, source_hashes=ex.hashes, posts=posts,
            content_hash=p.tree_hash(self.target), site_hash=p.site_hash(self.c), images=ex.image_summary()))
        return sid, ex

    def test_source_image_changes_require_new_preview(self):
        sid, _ = self.snapshot()
        p.validate_snapshot(self.c, sid)
        Image.new('RGB', (300, 200), 'blue').save(self.vault / 'photo.jpg', quality=98)
        with self.assertRaisesRegex(p.PublishError, '发生了变化'):
            p.validate_snapshot(self.c, sid)

    def test_compressed_snapshot_tampering_is_rejected(self):
        sid, ex = self.snapshot()
        self.exported(ex, 'photo.jpg').write_bytes(b'unreviewed')
        with self.assertRaisesRegex(p.PublishError, '快照'):
            p.validate_snapshot(self.c, sid)


@unittest.skipUnless(shutil.which('hugo') and shutil.which('git'), 'Requires Hugo and Git')
class ImagePublishingIntegrationTests(unittest.TestCase):
    def test_hugo_preview_and_git_publish_use_identical_compressed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root, vault, work, remote = [base / name for name in ('site', 'vault', 'work', 'remote.git')]
            root.mkdir(); vault.mkdir(); work.mkdir()
            for name in ('.gitignore', 'hugo.yaml', 'content', 'layouts', 'assets', 'static', 'themes'):
                source = p.ROOT / name
                if source.is_dir(): shutil.copytree(source, root / name)
                elif source.is_file(): shutil.copy2(source, root / name)
            p.run(['git', 'init', '--bare', remote], base)
            p.run(['git', 'init', '-b', 'main'], root)
            p.run(['git', 'config', 'user.name', 'Publisher Test'], root)
            p.run(['git', 'config', 'user.email', 'test@example.invalid'], root)
            p.run(['git', 'config', 'commit.gpgsign', 'false'], root)
            p.run(['git', 'add', '.'], root)
            p.run(['git', 'commit', '-m', 'Local integration fixture'], root)
            p.run(['git', 'remote', 'add', 'origin', remote], root)
            p.run(['git', 'push', '-u', 'origin', 'main'], root)
            (vault / 'post.md').write_text('---\ntitle: Image integration\nslug: image-integration\ndraft: false\n---\n![Photo](photo.jpg)\n![Diagram](diagram.png)')
            Image.new('RGB', (3000, 1500), 'steelblue').save(vault / 'photo.jpg', quality=98)
            Image.new('RGBA', (400, 300), (20, 90, 170, 128)).save(vault / 'diagram.png', compress_level=0)
            original_hashes = {f.name: p.digest(f) for f in vault.iterdir()}
            c = dict(root=root, vault=vault, work=work, hugo=shutil.which('hugo'),
                     base_url='https://example.invalid/blog/', repository='local/test')
            preview = p.preview(c, ['post.md'], serve=False)
            self.assertEqual(preview['images']['optimized'], 2)
            folder = work / 'snapshots' / preview['snapshot']
            html = (folder / 'public/posts/image-integration/index.html').read_text()
            images = list((folder / 'content/posts/image-integration/assets').iterdir())
            self.assertEqual(len(images), 2)
            for image in images:
                self.assertIn('assets/' + image.name, html)
                self.assertEqual(image.read_bytes(), (folder / 'public/posts/image-integration/assets' / image.name).read_bytes())
            p.publish(c, preview['snapshot'])
            for image in images:
                relative = 'content/posts/image-integration/assets/' + image.name
                pushed = subprocess.check_output(['git', '--git-dir', str(remote), 'show', 'main:' + relative])
                self.assertEqual(image.read_bytes(), pushed)
                self.assertEqual(image.read_bytes(), (root / relative).read_bytes())
                self.assertEqual(image.read_bytes(), (folder / 'production/posts/image-integration/assets' / image.name).read_bytes())
            self.assertEqual(original_hashes, {f.name: p.digest(f) for f in vault.iterdir()})


if __name__ == '__main__':
    unittest.main()
