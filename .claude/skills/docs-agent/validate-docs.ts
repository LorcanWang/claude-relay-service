import { readFileSync, existsSync, readdirSync, statSync } from 'fs';
import { join, extname, relative, basename } from 'path';

const DOCS_DIR = join(process.cwd(), 'docs');

// ── Helpers ────────────────────────────────────────────────────────────────────

function getAllMdFiles(dir: string, base: string = dir): { path: string; rel: string }[] {
  const results: { path: string; rel: string }[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry.startsWith('.')) continue;
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      results.push(...getAllMdFiles(full, base));
    } else if (extname(entry) === '.md') {
      results.push({ path: full, rel: relative(base, full) });
    }
  }
  return results;
}

function parseFrontmatter(content: string): Record<string, string> | null {
  if (!content.startsWith('---\n')) return null;
  const endIdx = content.indexOf('\n---', 4);
  if (endIdx === -1) return null;
  const fm: Record<string, string> = {};
  const block = content.slice(4, endIdx);
  for (const line of block.split('\n')) {
    const match = line.match(/^(\w+):\s*(.+)$/);
    if (match) fm[match[1]] = match[2];
  }
  return fm;
}

// ── Checks ─────────────────────────────────────────────────────────────────────

let warnings = 0;

function warn(file: string, msg: string) {
  console.log(`  ⚠ ${file}: ${msg}`);
  warnings++;
}

function checkFrontmatter(files: { path: string; rel: string }[]) {
  console.log('Check 1: YAML frontmatter...');
  for (const file of files) {
    const content = readFileSync(file.path, 'utf-8');
    const fm = parseFrontmatter(content);
    if (!fm) {
      warn(file.rel, 'Missing frontmatter');
      continue;
    }
    if (!fm.title) warn(file.rel, 'Missing title in frontmatter');
    if (!fm.tags) warn(file.rel, 'Missing tags in frontmatter');
    if (!fm.status) warn(file.rel, 'Missing status in frontmatter');
  }
}

function checkMOC(files: { path: string; rel: string }[]) {
  console.log('Check 2: MOC coverage...');
  const mocPath = join(DOCS_DIR, 'MOC.md');
  if (!existsSync(mocPath)) {
    warn('MOC.md', 'MOC file does not exist — run compile-vault.ts first');
    return;
  }
  const mocContent = readFileSync(mocPath, 'utf-8');
  for (const file of files) {
    const slug = basename(file.rel, extname(file.rel));
    if (slug === 'MOC') continue;
    if (!mocContent.includes(`[[${slug}`)) {
      warn(file.rel, 'Not listed in MOC.md');
    }
  }
}

function checkWikilinks(files: { path: string; rel: string }[]) {
  console.log('Check 3: Wikilink integrity...');
  const slugs = new Set(files.map(f => basename(f.rel, extname(f.rel))));

  for (const file of files) {
    const content = readFileSync(file.path, 'utf-8');
    const linkRegex = /\[\[([^|\]]+)(?:\|[^\]]+)?\]\]/g;
    let match;
    while ((match = linkRegex.exec(content)) !== null) {
      const target = match[1];
      if (!slugs.has(target)) {
        warn(file.rel, `Broken wikilink: [[${target}]]`);
      }
    }
  }
}

function checkDuplicateAliases(files: { path: string; rel: string }[]) {
  console.log('Check 4: Duplicate aliases...');
  const aliasMap = new Map<string, string>();
  for (const file of files) {
    const content = readFileSync(file.path, 'utf-8');
    const fm = parseFrontmatter(content);
    if (!fm?.aliases) continue;
    const aliases = fm.aliases.replace(/[\[\]"]/g, '').split(',').map(a => a.trim());
    for (const alias of aliases) {
      if (aliasMap.has(alias) && aliasMap.get(alias) !== file.rel) {
        warn(file.rel, `Duplicate alias "${alias}" (also in ${aliasMap.get(alias)})`);
      }
      aliasMap.set(alias, file.rel);
    }
  }
}

// ── Main ───────────────────────────────────────────────────────────────────────

function main() {
  console.log('Validating docs/ knowledge base...\n');

  const files = getAllMdFiles(DOCS_DIR);
  console.log(`Found ${files.length} markdown files\n`);

  checkFrontmatter(files);
  checkMOC(files);
  checkWikilinks(files);
  checkDuplicateAliases(files);

  console.log('\n═══════════════════════════════════════');
  if (warnings === 0) {
    console.log('  ✓ All checks passed — 0 warnings');
  } else {
    console.log(`  ${warnings} warning(s) found`);
  }
  console.log('═══════════════════════════════════════');
}

main();
