const { Plugin, Modal, SuggestModal, Notice, Setting, PluginSettingTab, TFile } = require('obsidian');
const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');
const { shell } = require('electron');

const defaults = { siteDir: '', pythonPath: '', selectedNotes: [], excludedFolders: ['敏感信息'] };

class NotePicker extends SuggestModal {
  constructor(app, plugin, done) { super(app); this.plugin = plugin; this.done = done; this.setPlaceholder('搜索要加入发布列表的文章…'); }
  getSuggestions(query) {
    const q = query.toLowerCase();
    return this.app.vault.getMarkdownFiles().filter(f => this.plugin.allowed(f.path) &&
      !this.plugin.settings.selectedNotes.includes(f.path) && (f.path.toLowerCase().includes(q) || this.plugin.title(f).toLowerCase().includes(q)))
      .sort((a, b) => b.stat.mtime - a.stat.mtime).slice(0, 80);
  }
  renderSuggestion(file, el) {
    el.createEl('div', { text: this.plugin.title(file) });
    el.createEl('small', { text: file.path, cls: 'hugo-muted' });
  }
  async onChooseSuggestion(file) { await this.plugin.add(file); this.done(); }
}

class DraftModal extends Modal {
  constructor(app, plugin) { super(app); this.plugin = plugin; }
  onOpen() {
    this.titleEl.setText('新建博客草稿');
    let title = '';
    new Setting(this.contentEl).setName('文章标题').addText(t => t.setPlaceholder('一个想和别人讨论的问题').onChange(v => title = v));
    new Setting(this.contentEl).addButton(b => b.setButtonText('创建并打开').setCta().onClick(async () => {
      if (!title.trim()) return new Notice('请填写标题。');
      const file = await this.plugin.newDraft(title.trim());
      this.close();
      await this.app.workspace.getLeaf(false).openFile(file);
    }));
  }
  onClose() { this.contentEl.empty(); }
}

class PublisherModal extends Modal {
  constructor(app, plugin) { super(app); this.plugin = plugin; this.snapshot = null; this.busy = false; this.result = ''; this.pollTimer = null; }
  onOpen() { this.modalEl.addClass('hugo-publisher'); this.render(); }
  onClose() { if (this.pollTimer) clearTimeout(this.pollTimer); this.contentEl.empty(); }
  async job(fn) {
    if (this.busy) return;
    this.busy = true; this.render();
    try { await fn(); }
    catch (e) { this.result = e.message; new Notice(e.message, 9000); }
    finally { this.busy = false; this.render(); }
  }
  invalidate() { this.snapshot = null; this.result = ''; this.render(); }
  render() {
    this.titleEl.setText('发布到博客');
    const el = this.contentEl; el.empty();
    el.createEl('p', { text: '选定文章 → 查看预览 → 发布此预览', cls: 'hugo-lead' });
    el.createEl('p', { text: '只有列表中的文章和它们引用的附件会导出。移出列表只停止更新，不会删除已发布文章。', cls: 'hugo-muted' });
    const list = el.createDiv('hugo-notes');
    const notes = this.plugin.settings.selectedNotes;
    if (!notes.length) list.createEl('p', { text: '先添加一篇想与别人分享的文章。', cls: 'hugo-empty' });
    notes.forEach(notePath => {
      const file = this.app.vault.getAbstractFileByPath(notePath);
      const row = new Setting(list).setName(file instanceof TFile ? this.plugin.title(file) : '文章已移动或删除').setDesc(notePath);
      row.addButton(b => b.setButtonText('打开').setDisabled(this.busy || !(file instanceof TFile)).onClick(async () => {
        this.close(); await this.app.workspace.getLeaf(false).openFile(file);
      }));
      row.addButton(b => b.setButtonText('移出').setDisabled(this.busy).onClick(async () => {
        this.plugin.settings.selectedNotes = notes.filter(p => p !== notePath);
        await this.plugin.saveSettings(); this.invalidate();
      }));
    });
    const add = new Setting(el);
    add.addButton(b => b.setButtonText('添加文章…').setDisabled(this.busy).onClick(() => new NotePicker(this.app, this.plugin, () => this.invalidate()).open()));
    add.addButton(b => b.setButtonText('添加当前文章').setDisabled(this.busy).onClick(async () => {
      const f = this.app.workspace.getActiveFile();
      if (!f) return new Notice('请先打开一篇文章。');
      try { await this.plugin.add(f); this.invalidate(); } catch(e) { new Notice(e.message); }
    }));
    add.addButton(b => b.setButtonText('新建草稿').setDisabled(this.busy).onClick(() => { this.close(); new DraftModal(this.app, this.plugin).open(); }));
    if (this.snapshot) {
      const box = el.createDiv('hugo-preview-summary');
      box.createEl('strong', { text: `已预览 ${this.snapshot.posts.length} 篇文章 · ${this.snapshot.attachments} 个附件` });
      this.snapshot.posts.forEach(p => {
        const a = box.createEl('a', { text: p.title, href: this.snapshot.url + p.url });
        a.addEventListener('click', e => { e.preventDefault(); shell.openExternal(a.href); });
      });
      this.snapshot.warnings.forEach(w => box.createEl('p', { text: w, cls: 'hugo-warning' }));
    }
    if (this.result) el.createEl('div', { text: this.result, cls: 'hugo-result' });
    const actions = new Setting(el);
    actions.addButton(b => b.setButtonText(this.busy ? '正在处理…' : '生成预览').setCta().setDisabled(this.busy || !notes.length).onClick(() => this.job(async () => {
      this.snapshot = null;
      const request = this.plugin.requestPath();
      fs.writeFileSync(request, JSON.stringify({ vault: this.app.vault.adapter.getBasePath(), notes: this.plugin.settings.selectedNotes }));
      this.snapshot = await this.plugin.call('preview', ['--request', request]);
      this.result = '预览已生成。检查文章与图片后，可以发布此预览。';
      await shell.openExternal(this.snapshot.url + this.snapshot.posts[0].url);
    })));
    actions.addButton(b => b.setButtonText('发布此预览').setDisabled(this.busy || !this.snapshot || this.snapshot.posts.some(p => p.draft)).onClick(() => this.job(async () => {
      const published = await this.plugin.call('publish', ['--snapshot', this.snapshot.snapshot]);
      this.result = published.message;
      this.snapshot = null;
      this.poll(0);
    })));
    const secondary = new Setting(el);
    secondary.addButton(b => b.setButtonText('发布状态').setDisabled(this.busy).onClick(() => this.job(async () => this.showStatus(await this.plugin.call('status')))));
    secondary.addButton(b => b.setButtonText('打开博客').onClick(() => shell.openExternal(this.plugin.localConfig().base_url)));
    secondary.addButton(b => b.setButtonText('重试推送').setDisabled(this.busy).onClick(() => this.job(async () => {
      const r = await this.plugin.call('retry'); this.result = r.message; this.poll(0);
    })));
  }
  showStatus(r) {
    if (r.status === 'completed') this.result = r.conclusion === 'success' ? '发布完成，公开网站已更新。' : `网站部署未完成（${r.conclusion}），可在 GitHub Actions 查看原因。`;
    else this.result = 'GitHub 正在生成网站。你可以继续写作，稍后查看发布状态。';
    return r.status === 'completed';
  }
  poll(n) {
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(async () => {
      try {
        const done = this.showStatus(await this.plugin.call('status')); this.render();
        if (done) { if (this.result.startsWith('发布完成')) new Notice(this.result); }
        else if (n < 36) this.poll(n + 1);
      } catch (e) { this.result = e.message; this.render(); }
    }, 5000);
  }
}

class SettingsTab extends PluginSettingTab {
  constructor(app, plugin) { super(app, plugin); this.plugin = plugin; }
  display() {
    this.containerEl.empty();
    this.containerEl.createEl('h2', { text: 'Hugo 博客发布' });
    new Setting(this.containerEl).setName('博客项目文件夹').addText(t => t.setValue(this.plugin.settings.siteDir).onChange(async v => { this.plugin.settings.siteDir = v; await this.plugin.saveSettings(); }));
    new Setting(this.containerEl).setName('Python 路径').setDesc('使用安装时创建的专用环境。').addText(t => t.setValue(this.plugin.settings.pythonPath).onChange(async v => { this.plugin.settings.pythonPath = v; await this.plugin.saveSettings(); }));
  }
}

module.exports = class HugoPublisher extends Plugin {
  async onload() {
    this.settings = { ...defaults, ...(await this.loadData()) };
    this.addRibbonIcon('send', '打开博客发布面板', () => new PublisherModal(this.app, this).open());
    this.addCommand({ id: 'open-publisher', name: '打开发布面板', callback: () => new PublisherModal(this.app, this).open() });
    this.addCommand({ id: 'add-current-note', name: '将当前笔记加入发布列表', callback: async () => {
      try { const f = this.app.workspace.getActiveFile(); if (f) { await this.add(f); new PublisherModal(this.app, this).open(); } }
      catch(e) { new Notice(e.message); }
    } });
    this.addCommand({ id: 'new-draft', name: '新建博客草稿', callback: () => new DraftModal(this.app, this).open() });
    this.addCommand({ id: 'stop-preview', name: '关闭本地预览', callback: async () => { try { await this.call('stop'); new Notice('本地预览已关闭。'); } catch(e) { new Notice(e.message); } } });
    this.addSettingTab(new SettingsTab(this.app, this));
  }
  allowed(p) { return !p.split('/').some(s => s.startsWith('.') || this.settings.excludedFolders.includes(s)); }
  title(file) { return String(this.app.metadataCache.getFileCache(file)?.frontmatter?.title || (file.basename === 'index' ? file.parent.name : file.basename)); }
  async add(file) {
    if (!(file instanceof TFile) || file.extension !== 'md') throw new Error('请选择 Markdown 文章。');
    if (!this.allowed(file.path)) throw new Error('这份笔记所在的文件夹不参与博客发布。');
    if (!this.settings.selectedNotes.includes(file.path)) { this.settings.selectedNotes.push(file.path); await this.saveSettings(); }
  }
  async saveSettings() { await this.saveData(this.settings); }
  localConfig() {
    if (!this.settings.siteDir) throw new Error('请先在插件设置里配置博客文件夹。');
    return JSON.parse(fs.readFileSync(path.join(this.settings.siteDir, '.local.json'), 'utf8'));
  }
  requestPath() {
    const work = this.localConfig().work_dir; fs.mkdirSync(work, { recursive: true });
    return path.join(work, 'selection.json');
  }
  call(command, args = []) {
    const site = this.settings.siteDir;
    if (!site || !this.settings.pythonPath) return Promise.reject(new Error('博客路径未配置，请打开插件设置。'));
    return new Promise((resolve, reject) => {
      execFile(this.settings.pythonPath, [path.join(site, 'scripts', 'publisher.py'), command, ...args],
        { cwd: site, timeout: 240000, maxBuffer: 1024 * 1024, env: { ...process.env, PATH: '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:' + (process.env.PATH || '') } },
        (err, stdout, stderr) => {
          let result;
          try { result = JSON.parse(stdout.trim().split('\n').pop()); }
          catch (_) { return reject(new Error((stderr || err?.message || '发布工具没有返回结果。').slice(-2200))); }
          if (!result.ok) reject(new Error(result.error || '操作失败。')); else resolve(result);
        });
    });
  }
  async newDraft(title) {
    const stamp = new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14);
    const slug = `note-${stamp}`;
    const folder = `公开写作/文章/${slug}`;
    let parent = '';
    for (const segment of folder.split('/')) {
      parent = parent ? parent + '/' + segment : segment;
      if (!this.app.vault.getAbstractFileByPath(parent)) await this.app.vault.createFolder(parent);
    }
    const date = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date());
    const content = `---\ntitle: ${JSON.stringify(title)}\ndate: ${date}\nslug: ${slug}\ntags: []\ndescription: ""\ndraft: true\n---\n\n从一个具体的观察、经历或问题写起。\n\n## 我现在的想法\n\n\n\n## 想与你讨论\n\n留下一个你真正好奇、希望听到别人经验的问题。\n`;
    const file = await this.app.vault.create(folder + '/index.md', content);
    await this.add(file); return file;
  }
};
