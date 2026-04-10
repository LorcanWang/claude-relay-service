import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, statSync } from 'fs';
import { join, basename, extname, relative } from 'path';
import { execSync } from 'child_process';

const DOCS_DIR = join(process.cwd(), 'docs');
const OBSIDIAN_DIR = join(DOCS_DIR, '.obsidian');

// ── Category mappings ──────────────────────────────────────────────────────────
const CATEGORIES: Record<string, string[]> = {
  'Architecture': [
    'architecture', 'request-flow', 'clean-architecture', 'relay-service',
    'middleware', 'routing', 'streaming',
  ],
  'Accounts & Scheduling': [
    'scheduler', 'account', 'ccr', 'claude-official', 'claude-console',
    'gemini', 'openai', 'bedrock', 'azure', 'droid', 'sticky-session',
    'concurrency', 'scheduling',
  ],
  'Security': [
    'auth', 'encryption', 'api-key', 'token', 'security', 'permissions',
  ],
  'Operations': [
    'deployment', 'docker', 'redis', 'monitoring', 'troubleshooting',
    'pricing', 'cost', 'webhook',
  ],
  'Reviews & Decisions': [
    'review', 'pr-review', 'decision', 'findings',
  ],
};

// ── Keyword -> tag mappings ─────────────────────────────────────────────────────
const KEYWORD_TAGS: Record<string, string[]> = {
  'redis': ['redis'], 'proxy': ['proxy'], 'auth': ['auth'],
  'scheduler': ['scheduler'], 'ccr': ['ccr'], 'api key': ['api-key'],
  'encryption': ['encryption'], 'streaming': ['streaming'], 'sse': ['streaming'],
  'oauth': ['oauth'], 'jwt': ['jwt'], 'docker': ['docker'],
  'webhook': ['webhook'], 'rate limit': ['rate-limiting'], 'concurrency': ['concurrency'],
  'sticky session': ['sticky-session'], 'claude': ['claude'], 'gemini': ['gemini'],
  'openai': ['openai'], 'bedrock': ['bedrock'], 'azure': ['azure'],
  'droid': ['droid'], 'pricing': ['pricing'], 'cost': ['pricing'],
  'group': ['account-groups'], 'middleware': ['middleware'],
};

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

function hasFrontmatter(content: string): boolean {
  return content.startsWith('---\n') && content.indexOf('\n---', 4) !== -1;
}

function extractTitle(content: string): string {
  const match = content.match(/^#\s+(.+)$/m);
  return match ? match[1].trim() : '';
}

function getGitDate(filePath: string, mode: 'created' | 'updated'): string {
  try {
    const cmd = mode === 'created'
      ? `git log --format=%aI --diff-filter=A -- "${filePath}" | tail -1`
      : `git log -1 --format=%aI -- "${filePath}"`;
    const result = execSync(cmd, { cwd: process.cwd(), encoding: 'utf-8' }).trim();
    return result ? result.slice(0, 10) : new Date().toISOString().slice(0, 10);
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}

function slugFromFilename(filename: string): string {
  return basename(filename, extname(filename));
}

function detectTags(slug: string, content: string): string[] {
  const tags = new Set<string>();

  // From filename segments
  for (const seg of slug.toLowerCase().replace(/_/g, '-').split('-')) {
    if (seg.length > 2) {
      for (const [kw, kwTags] of Object.entries(KEYWORD_TAGS)) {
        if (seg.includes(kw.toLowerCase())) kwTags.forEach(t => tags.add(t));
      }
    }
  }

  // From content keywords
  const lowerContent = content.toLowerCase().slice(0, 3000);
  for (const [kw, kwTags] of Object.entries(KEYWORD_TAGS)) {
    if (lowerContent.includes(kw.toLowerCase())) kwTags.forEach(t => tags.add(t));
  }

  // Type tag from filename patterns
  if (slug.match(/spec/i)) tags.add('spec');
  if (slug.match(/design/i)) tags.add('design');
  if (slug.match(/review/i)) tags.add('review');
  if (slug.match(/guide/i)) tags.add('guide');
  if (slug.match(/findings/i)) tags.add('findings');
  if (slug.match(/troubleshoot/i)) tags.add('troubleshooting');

  return [...tags].sort();
}

function detectCategory(slug: string, rel: string): string {
  if (rel.startsWith('reviews/')) return 'Reviews & Decisions';
  for (const [cat, patterns] of Object.entries(CATEGORIES)) {
    for (const pattern of patterns) {
      if (slug.toLowerCase().includes(pattern.toLowerCase())) return cat;
    }
  }
  return 'Other';
}

// ── Step 1: Create .obsidian config ────────────────────────────────────────────

function ensureObsidianConfig() {
  if (!existsSync(OBSIDIAN_DIR)) {
    mkdirSync(OBSIDIAN_DIR, { recursive: true });
  }
  const appJson = join(OBSIDIAN_DIR, 'app.json');
  if (!existsSync(appJson)) {
    writeFileSync(appJson, JSON.stringify({ strictLineBreaks: false, showFrontmatter: false }, null, 2));
  }

  // Add to .gitignore if not already
  const gitignorePath = join(process.cwd(), '.gitignore');
  if (existsSync(gitignorePath)) {
    const gitignore = readFileSync(gitignorePath, 'utf-8');
    if (!gitignore.includes('docs/.obsidian')) {
      writeFileSync(gitignorePath, gitignore.trimEnd() + '\n\n# Obsidian local config\ndocs/.obsidian/\n');
      console.log('  Added docs/.obsidian/ to .gitignore');
    }
  }
}

// ── Step 1b: Ensure CLAUDE.md has knowledge base section ───────────────────────

function ensureClaudeMd() {
  const claudeMdPath = join(process.cwd(), 'CLAUDE.md');
  const knowledgeMarker = '## Knowledge Base';
  const knowledgeSection = `## Knowledge Base

- Before working on any feature, check \`docs/MOC.md\` and read the relevant doc.
- After implementing a new feature or making architectural changes, update or create a doc in \`docs/\` following the Obsidian conventions (YAML frontmatter, wikilinks, tags). See \`.claude/skills/docs-agent/SKILL.md\` for the full guide.`;

  if (existsSync(claudeMdPath)) {
    const content = readFileSync(claudeMdPath, 'utf-8');
    if (content.includes(knowledgeMarker)) return;
    // Insert after the first heading line
    const firstNewline = content.indexOf('\n');
    const updated = content.slice(0, firstNewline + 1) + '\n' + knowledgeSection + '\n' + content.slice(firstNewline + 1);
    writeFileSync(claudeMdPath, updated);
  } else {
    writeFileSync(claudeMdPath, `# CLAUDE.md\n\n${knowledgeSection}\n`);
  }
  console.log('  Updated CLAUDE.md with Knowledge Base section');
}

// ── Step 2: Add frontmatter ────────────────────────────────────────────────────

interface DocInfo {
  path: string;
  rel: string;
  slug: string;
  title: string;
  aliases: string[];
  tags: string[];
  category: string;
}

function addFrontmatter(files: { path: string; rel: string }[]): { docs: DocInfo[]; added: number } {
  const docs: DocInfo[] = [];
  let added = 0;

  for (const file of files) {
    const content = readFileSync(file.path, 'utf-8');
    const slug = slugFromFilename(file.rel);
    const title = extractTitle(content) || slug.replace(/[-_]/g, ' ');
    const aliases = [slug];
    const tags = detectTags(slug, content);
    const category = detectCategory(slug, file.rel);

    docs.push({ path: file.path, rel: file.rel, slug, title, aliases, tags, category });

    if (hasFrontmatter(content)) continue;

    const created = getGitDate(file.path, 'created');
    const updated = getGitDate(file.path, 'updated');

    const fm = [
      '---',
      `title: "${title.replace(/"/g, '\\"')}"`,
      `aliases: [${aliases.map(a => `"${a}"`).join(', ')}]`,
      `tags: [${tags.join(', ')}]`,
      `created: ${created}`,
      `updated: ${updated}`,
      'status: active',
      '---',
      '',
    ].join('\n');

    writeFileSync(file.path, fm + content);
    added++;
  }

  return { docs, added };
}

// ── Step 3: Insert wikilinks ───────────────────────────────────────────────────

function insertWikilinks(docs: DocInfo[]): number {
  let totalLinks = 0;

  // Build lookup: title/alias -> slug
  const titleMap = new Map<string, string>();
  for (const doc of docs) {
    titleMap.set(doc.title.toLowerCase(), doc.slug);
    for (const alias of doc.aliases) {
      titleMap.set(alias.toLowerCase(), doc.slug);
    }
  }

  for (const doc of docs) {
    let content = readFileSync(doc.path, 'utf-8');

    // Split into frontmatter + body
    let fm = '';
    let body = content;
    if (hasFrontmatter(content)) {
      const endIdx = content.indexOf('\n---', 4);
      fm = content.slice(0, endIdx + 4);
      body = content.slice(endIdx + 4);
    }

    let linksAdded = 0;

    for (const [matchText, targetSlug] of titleMap) {
      if (targetSlug === doc.slug) continue;
      if (matchText.length < 4) continue;

      if (body.includes(`[[${targetSlug}`)) continue;

      const escapedText = matchText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const regex = new RegExp(`(?<!\\[\\[)(?<!\\[)(?<!\`)\\b(${escapedText})\\b(?!\\]\\])(?!\\])(?!\`)`, 'i');
      const match = body.match(regex);

      if (match && match.index !== undefined) {
        const before = body.slice(0, match.index);
        const codeBlockCount = (before.match(/```/g) || []).length;
        if (codeBlockCount % 2 === 1) continue;
        const lastOpenBracket = before.lastIndexOf('[');
        const lastCloseBracket = before.lastIndexOf(']');
        if (lastOpenBracket > lastCloseBracket) continue;

        const original = match[1];
        const wikilink = `[[${targetSlug}|${original}]]`;
        body = body.slice(0, match.index) + wikilink + body.slice(match.index + original.length);
        linksAdded++;
      }
    }

    if (linksAdded > 0) {
      writeFileSync(doc.path, fm + body);
      totalLinks += linksAdded;
    }
  }

  return totalLinks;
}

// ── Step 4: Generate MOC ───────────────────────────────────────────────────────

function generateMOC(docs: DocInfo[]): void {
  const grouped: Record<string, DocInfo[]> = {};
  for (const doc of docs) {
    if (doc.slug === 'MOC') continue;
    if (!grouped[doc.category]) grouped[doc.category] = [];
    grouped[doc.category].push(doc);
  }

  const order = ['Architecture', 'Accounts & Scheduling', 'Security', 'Operations', 'Reviews & Decisions', 'Other'];
  const sortedCategories = Object.keys(grouped).sort((a, b) => {
    const ai = order.indexOf(a);
    const bi = order.indexOf(b);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  const tagCounts = new Map<string, number>();
  for (const doc of docs) {
    for (const tag of doc.tags) {
      tagCounts.set(tag, (tagCounts.get(tag) || 0) + 1);
    }
  }

  const lines = [
    '---',
    'title: "Map of Content"',
    'aliases: ["MOC", "index"]',
    'tags: [moc]',
    `updated: ${new Date().toISOString().slice(0, 10)}`,
    'status: active',
    '---',
    '',
    '# Map of Content',
    '',
    `> Knowledge base for Claude Relay Service — ${docs.filter(d => d.slug !== 'MOC').length} documents`,
    '',
  ];

  for (const cat of sortedCategories) {
    lines.push(`## ${cat}`, '');
    const sorted = grouped[cat].sort((a, b) => a.title.localeCompare(b.title));
    for (const doc of sorted) {
      const tagStr = doc.tags.length > 0 ? ` \`${doc.tags.slice(0, 3).join('` `')}\`` : '';
      lines.push(`- [[${doc.slug}|${doc.title}]]${tagStr}`);
    }
    lines.push('');
  }

  lines.push('## Tags', '');
  const sortedTags = [...tagCounts.entries()].sort((a, b) => b[1] - a[1]);
  lines.push(sortedTags.map(([tag, count]) => `\`${tag}\` (${count})`).join(' · '));
  lines.push('');

  writeFileSync(join(DOCS_DIR, 'MOC.md'), lines.join('\n'));
}

// ── Main ───────────────────────────────────────────────────────────────────────

function main() {
  console.log('Compiling docs/ into Obsidian vault...\n');

  console.log('Step 1: Ensuring .obsidian/ config & CLAUDE.md...');
  ensureObsidianConfig();
  ensureClaudeMd();

  const mdFiles = getAllMdFiles(DOCS_DIR);
  console.log(`  Found ${mdFiles.length} markdown files\n`);

  console.log('Step 2: Adding YAML frontmatter...');
  const { docs, added } = addFrontmatter(mdFiles);
  console.log(`  Added frontmatter to ${added} files\n`);

  console.log('Step 3: Inserting wikilinks...');
  const linksInserted = insertWikilinks(docs);
  console.log(`  Inserted ${linksInserted} wikilinks\n`);

  console.log('Step 4: Generating MOC...');
  generateMOC(docs);
  console.log(`  Generated docs/MOC.md\n`);

  console.log('═══════════════════════════════════════');
  console.log(`  Files processed:    ${mdFiles.length}`);
  console.log(`  Frontmatter added:  ${added}`);
  console.log(`  Wikilinks inserted: ${linksInserted}`);
  console.log(`  MOC generated:      docs/MOC.md`);
  console.log('═══════════════════════════════════════');
}

main();
