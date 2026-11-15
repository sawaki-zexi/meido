import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, expect, it } from 'vitest';
import { MemoryDocuments } from './MemoryDocuments';

afterEach(cleanup);

const source = 'consolidation:role-private:12-24:0123456789abcdef0123';

it('按章节和列表阅读文档，宿主来源只呈现已知的消息范围', () => {
  const content = [
    '# 关于主人', '', '## 喜欢的事', '', '- [preference] 喜欢红茶。',
    `  <!-- source: ${source} -->`, '', '<!-- meido-optimizer:start -->',
    '## 一起的约定', '', '- 完成任务 MEIDO-2026-353，订单 550e8400-e29b-41d4-a716-446655440000。',
    '<!-- meido-optimizer:end -->', '', '> 引用：第 42 次任务保留编号 #353。',
  ].join('\n');
  render(<MemoryDocuments documents={{ 'MEMORY.md': content }} journals={[]} selected="MEMORY.md" onSelect={() => {}} />);
  const paper = within(screen.getByRole('article', { name: '长期记忆' }));
  expect(paper.getByRole('heading', { name: '关于主人' })).toBeVisible();
  expect(paper.getByRole('heading', { name: '喜欢的事' })).toBeVisible();
  expect(paper.getAllByRole('listitem')).toHaveLength(2);
  expect(paper.getByText('消息 12–24', { exact: false })).toBeVisible();
  expect(paper.getByText(/MEIDO-2026-353，订单 550e8400-e29b-41d4-a716-446655440000/)).toBeVisible();
  expect(paper.getByText('引用：第 42 次任务保留编号 #353。')).toBeVisible();
  expect(paper.queryByText(/role-private|<!-- source:|meido-optimizer/)).not.toBeInTheDocument();
});

it('保留围栏、缩进和行内代码示例及普通 HTML 文字，不启用 HTML', () => {
  const content = [
    '# 格式示例', '', '```html', `<!-- source: ${source} -->`,
    '<!-- recent-context-sources: {"example":"保留"} -->', '```', '',
    '    <!-- source: indented-example -->', '',
    '行内示例 `<!-- source: inline-example -->`。', '',
    '> <!-- source: quoted-example -->', '',
    '<div data-example="353">普通 HTML 文字</div>', '',
    '<!-- 普通注释也要保留 -->', '', '<script>window.untrusted = true</script>',
  ].join('\n');
  const { container } = render(<MemoryDocuments documents={{ 'MEMORY.md': content }} journals={[]} selected="MEMORY.md" onSelect={() => {}} />);
  const paper = within(screen.getByRole('article', { name: '长期记忆' }));
  expect(paper.getByText(`<!-- source: ${source} -->`, { exact: false, selector: 'code' })).toBeVisible();
  expect(paper.getByText('<!-- source: indented-example -->', { exact: false, selector: 'code' })).toBeVisible();
  expect(paper.getByText('<!-- source: inline-example -->', { selector: 'code' })).toBeVisible();
  expect(paper.getByText('<!-- source: quoted-example -->')).toBeVisible();
  expect(screen.getByRole('article', { name: '长期记忆' })).toHaveTextContent('<div data-example="353">普通 HTML 文字</div>');
  expect(screen.getByRole('article', { name: '长期记忆' })).toHaveTextContent('<!-- 普通注释也要保留 -->');
  expect(container.querySelector('[data-example], script')).toBeNull();
});

it('兼容待整理旧格式和近期来源注释，没有定位信息时不给出跳转或日期', async () => {
  const user = userEvent.setup();
  function Documents() {
    const [selected, select] = useState('PENDING.md');
    return <MemoryDocuments documents={{
      'PENDING.md': `# 待整理\n\n- [preference] 喜欢红茶 (来源：整理 ${source})\n  <!-- source: candidate-private -->\n\n- [fact] 门牌 353（来源：消息 42）\n  <!-- source: legacy-private -->\n\n- [key_info] 业务编号 KEY-353 不能被隐藏。`,
      'RECENT_CONTEXT.md': '<!-- recent-context-sources: {"role:private":{"messageIds":["message-private"],"fingerprint":"private-hash"}} -->\n\n## 还在聊的事\n\n旅行安排。\n\n<!-- recent-raw -->\n## 最近的对话\n\n- [user] 周末去看海。',
    }} journals={[]} selected={selected} onSelect={select} />;
  }
  render(<Documents />);
  const pending = within(screen.getByRole('article', { name: '待整理候选' }));
  expect(pending.getByText(/喜欢红茶/)).toBeVisible();
  expect(pending.getByText(/消息 12–24/)).toBeVisible();
  expect(pending.getByText(/门牌 353（来源：消息 42）/)).toBeVisible();
  expect(pending.getByText(/重要信息 ·/)).toBeVisible();
  expect(pending.getByText(/业务编号 KEY-353 不能被隐藏/)).toBeVisible();
  expect(pending.queryByText(/consolidation:|candidate-private|legacy-private/)).not.toBeInTheDocument();
  expect(pending.getByText('这份文档未提供可直接定位的原文入口。')).toBeVisible();
  expect(pending.queryByRole('link')).not.toBeInTheDocument();
  expect(pending.queryByRole('button')).not.toBeInTheDocument();
  expect(pending.queryByText(/2026年|2026-\d\d/)).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /近期上下文/ }));
  const recent = within(screen.getByRole('article', { name: '近期上下文' }));
  expect(recent.getByRole('heading', { name: '还在聊的事' })).toBeVisible();
  expect(recent.getByText(/周末去看海/)).toBeVisible();
  expect(recent.queryByText(/recent-context-sources|recent-raw|message-private|private-hash/)).not.toBeInTheDocument();
});

it('可浏览五类文档与整理日志，空文档有清晰提示', async () => {
  const user = userEvent.setup();
  function Documents() {
    const [selected, select] = useState('MEMORY.md');
    return <MemoryDocuments documents={{ 'SELF.md': '## 我的性格\n\n喜欢安静地阅读。' }} journals={[
      { date: '2026-11-09', content: '## 本次整理\n\n- 重新整理了旅行偏好。\n  <!-- source: journal-private -->' },
    ]} selected={selected} onSelect={select} />;
  }
  render(<Documents />);
  expect(screen.getByText('这里还没有内容')).toBeVisible();
  const navigation = within(screen.getByRole('navigation', { name: '文档与整理日志' }));
  expect(navigation.getAllByRole('button')).toHaveLength(6);
  await user.click(navigation.getByRole('button', { name: /自我认识/ }));
  expect(screen.getByRole('heading', { name: '我的性格' })).toBeVisible();
  await user.click(navigation.getByRole('button', { name: /整理日志.*2026/ }));
  expect(screen.getByRole('heading', { name: '本次整理' })).toBeVisible();
  expect(screen.getByText('重新整理了旅行偏好。')).toBeVisible();
  expect(screen.queryByText(/journal-private/)).not.toBeInTheDocument();
});

it('只有宿主标记的文档显示空内容，日期未知的旧日志不使用文件名充当日期', () => {
  render(<MemoryDocuments documents={{ 'MEMORY.md': '<!-- recent-context-sources: {} -->\n\n<!-- recent-raw -->' }} journals={[
    { date: 'log-private-key', content: '保留下来的日志正文。' },
  ]} selected="MEMORY.md" onSelect={() => {}} />);
  expect(screen.getByText('这里还没有内容')).toBeVisible();
  expect(screen.getByRole('button', { name: '整理日志（日期未记录）' })).toBeVisible();
  expect(screen.queryByText(/log-private-key|recent-context-sources|recent-raw/)).not.toBeInTheDocument();
});

it('当前近期文档的预览注释不显示，真实整理截止时间转为日期，普通 until 文字保留', () => {
  render(<MemoryDocuments documents={{ 'RECENT_CONTEXT.md': '# 最近发生的事\n\n## 最近聊过的事\nuntil: 2026-11-09T12:40:00+00:00\n- 最近在计划旅行。\n\n## 最近的对话\n<!-- a-preview = assistant reply preview only -->\n- [user] 还想看海。\n\n## 格式说明\n\nuntil: PROJECT-353' }} journals={[]} selected="RECENT_CONTEXT.md" onSelect={() => {}} />);
  const article = screen.getByRole('article', { name: '近期上下文' });
  expect(within(article).getByText(/整理至：2026/)).toBeVisible();
  expect(within(article).getByText(/最近在计划旅行/)).toBeVisible();
  expect(within(article).getByText(/还想看海/)).toBeVisible();
  expect(article).toHaveTextContent('until: PROJECT-353');
  expect(article).not.toHaveTextContent('a-preview');
  expect(article).not.toHaveTextContent('2026-11-09T12:40:00+00:00');
});

it.each(['none', 'unknown-private-cutoff', '2026-02-30T12:40:00+00:00'])('近期整理时间 %s 没有有效日期时不制造日期', value => {
  render(<MemoryDocuments documents={{ 'RECENT_CONTEXT.md': `## 最近聊过的事\nuntil: ${value}\n- 正在计划旅行。` }} journals={[]} selected="RECENT_CONTEXT.md" onSelect={() => {}} />);
  const article = screen.getByRole('article', { name: '近期上下文' });
  expect(within(article).getByText('整理时间未记录')).toBeVisible();
  expect(article).not.toHaveTextContent(value);
  expect(article).not.toHaveTextContent('整理至：');
});
