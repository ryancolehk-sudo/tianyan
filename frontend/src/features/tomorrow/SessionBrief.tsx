import { displayTime, text } from './model';
import type { ReportView } from './model';

type Row = Record<string, unknown>;
function numeric(v: unknown): number | null {
  if (typeof v !== 'number' && (typeof v !== 'string' || !v.trim())) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
function pct(n: number) { return n.toFixed(2).replace(/\.?0+$/, '') + '%'; }
function clock(raw: unknown) {
  const digits = String(raw ?? '').padStart(6, '0');
  return raw && /^\d{6}$/.test(digits) ? digits.slice(0, 2) + ':' + digits.slice(2, 4) : null;
}
type Observation = { title: string; why: string; verify: string; reject: string };

export function closingBrief(report: ReportView) {
  const market = report.dossier.market as { breadth?: Row; turnover?: Row; limits?: {supplier_reported?: Row} } | undefined;
  const breadth = market?.breadth;
  const share = numeric(breadth?.up_share_pct);
  const falling = numeric(breadth?.down);
  const rising = numeric(breadth?.up);
  const chapter = (key: string) => report.sections.find(s => s.key === key)?.rows ?? [];
  const sectorRows = chapter('sectors');
  const withBreadth = sectorRows.filter(s => (numeric(s['上涨成分占比（%）']) ?? -1) > 50 && (numeric(s['当日涨跌幅（%）']) ?? 0) > 0 && (numeric(s['资金净额（元）']) ?? 0) > 0);
  const strongest = withBreadth.sort((a,b) => Number(b['当日涨跌幅（%）']) - Number(a['当日涨跌幅（%）']))[0];
  const narrow = sectorRows.find(s => numeric(s['上涨成分占比（%）']) !== null && Number(s['上涨成分占比（%）']) < 50 && Number(s['当日涨跌幅（%）']) > 0);
  const leaders = chapter('limits').filter(s => s['分类'] === '涨停' && (numeric(s['连板数']) ?? 0) >= 2)
    .sort((a,b) => Number(b['连板数']) - Number(a['连板数']) || Number(a['开板次数']) - Number(b['开板次数']));
  const leader = leaders[0];
  const indices = chapter('market');
  const atLow = indices.filter(r => Number(r['已覆盖交易日']) === 20 && numeric(r['收盘']) !== null && Number(r['收盘']) === numeric(r['近20日收盘最低']));
  if (share === null || rising === null || falling === null) return null;
  const weak = share < 100 / 3, strong = share > 200 / 3;
  const title = weak ? '普跌之后，先看反弹能否扩散' : strong ? '普涨之后，先看强势能否延续' : '涨跌分化，先看哪一侧扩大优势';
  const checks: Observation[] = [];
  const volumeChange = numeric(market?.turnover?.change_pct);
  const index = atLow.find(r => r['指数'] === '创业板指') ?? atLow[0];
  const floor = index ? text(index['指数']) + ' ' + Number(index['收盘']).toFixed(2) + ' 点' : '';
  checks.push({
    title: weak ? '指数反弹，有多少股票跟上？' : '上涨是否得到多数股票响应？',
    why: floor ? atLow.map(r => text(r['指数'])).join('、') + '均收在各自近 20 个交易日最低收盘位。' : '以上涨与下跌家数核对指数方向，观察涨势的覆盖面。',
    verify: '10:00 同口径上涨家数多于下跌家数' + (floor ? '，且' + floor + '仍守住' : '') + '，再把普遍走强列为首要情景。',
    reject: floor ? '指数跌破上述收盘位，且下跌家数仍占多数，继续按弱势观察。' : '下跌家数反超上涨家数，将普遍走强情景降级。'
  });
  if (strongest) {
    const name = text(strongest['板块']);
    const names = text(strongest['关联观察标的']).split(/[；、]/).slice(0,2).join('、');
    checks.push({title: name + '的强度能否延续？',
      why: '板块涨 ' + pct(Number(strongest['当日涨跌幅（%）'])) + '，已覆盖成分中 ' + pct(Number(strongest['上涨成分占比（%）'])) + ' 上涨，资金净流入 ' + (Number(strongest['资金净额（元）']) / 1e8).toFixed(2) + ' 亿元。',
      verify: '10:00 板块仍红盘，过半已覆盖成分上涨。' + (names !== '—' ? '用' + names + '交叉观察。' : ''),
      reject: '板块翻绿且上涨成分降至半数以下，撤下这个优先观察方向。'
    });
  }
  if (leader) {
    const name = text(leader['标的']), height = Number(leader['连板数']);
    const peer = leaders.find(s => Number(s['连板数']) < height);
    const opened = numeric(leader['开板次数']), last = clock(leader['最后封板']);
    checks.push({title: name + '能否带动梯队？',
      why: name + '收出 ' + height + ' 连板' + (opened === null ? '' : '，盘中开板 ' + opened + ' 次') + (last ? '，最后封板在 ' + last : '') + '。' + (opened !== null && opened > 0 ? '高度领先，但封板有分歧。' : ''),
      verify: '开盘半小时核对' + name + '能否封住涨停' + (peer ? '，并看' + text(peer['标的']) + '等低一梯队是否同步走强' : '') + '。',
      reject: '到 10:00 仍未封板' + (peer ? '且下一梯队走弱' : '') + '，放弃连板接力扩散的假设。'
    });
  }
  return {title, falling, rising, share, volumeChange, narrow, strongest, checks};
}

export function SessionBrief({ report }: { report: ReportView }) {
  const brief = closingBrief(report);
  if (!brief) return <section className="tp-panel"><h2>开盘观察预案</h2><p>本稿缺少同一交易日的涨跌家数或上涨占比，无法判断市场涨跌面。</p></section>;
  const {title, falling, rising, share, volumeChange, narrow, strongest, checks} = brief;
  return <section className="tp-brief" aria-label="开盘观察预案"><header><p>开盘观察预案 · {report.target_trade_day ?? '下一交易日'}</p><h2>{title}</h2>
    <div>{report.source_trade_day} 收盘，{falling.toLocaleString('zh-CN')} 只下跌、{rising.toLocaleString('zh-CN')} 只上涨，上涨占比 {pct(share)}{volumeChange !== null ? '，成交额较前一日' + (volumeChange < 0 ? '减少 ' : '增加 ') + pct(Math.abs(volumeChange)) : ''}。</div></header>
    {narrow && strongest && <p className="tp-contrast"><b>容易误读的强势</b>　“{text(narrow['板块'])}”涨 {pct(Number(narrow['当日涨跌幅（%）']))}，但只有 {pct(Number(narrow['上涨成分占比（%）']))} 的已覆盖成分上涨。只看板块涨幅，会高估这组股票的普遍强度。</p>}
    <div className="tp-observations">{checks.map(c => <article key={c.title}><h3>{c.title}</h3><p>{c.why}</p><dl><dt>支持条件</dt><dd>{c.verify}</dd><dt>放弃条件</dt><dd>{c.reject}</dd></dl></article>)}</div>
    <footer>依据 {report.source_trade_day} 收盘数据 · 本稿资料截止 {displayTime(report.as_of_time)}</footer></section>;
}
