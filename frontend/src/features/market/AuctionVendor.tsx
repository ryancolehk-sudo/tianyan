import { useCallback, useEffect, useId, useMemo, useState } from 'react';
import type { CSSProperties } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { useSubmit } from '../../shared/useSubmit';
import { Empty, ErrorState, Segmented } from '../../shared/ui';
import { Icon } from '../../shared/icons';
import { AUCTION_PAGE_SIZE, AUCTION_STAGES, percent, reasonText, sourceStates, stamp, today } from './auctionFormat';
import { VENDOR_FAMILIES, VENDOR_FAMILY_LABEL } from './auctionVendorBatches';
import type { VendorBatch, VendorStock } from './auctionVendorBatches';
import { allVendorBatches, compactMoney, latestStage, matchesStock, sortStocks } from './auctionWorkbenchModel';
import { useAuctionCache } from './useAuctionCache';
import { AuctionSealChart } from './AuctionSealChart';
import { AuctionObservationPool, usePreviousAuctionDay } from './AuctionObservationPool';
import { HistoryChart } from './HistoryChart';
import './auctionWorkbench.css';

function StockHistory({ stock }: { stock: VendorStock }) {
  const read = useRead(useCallback(() => api.instruments(stock.code), [stock.code]));
  const candidates = read.data?.data.items.filter(i => i.symbol === stock.code && i.asset_type === 'stock') ?? [];
  return read.busy ? <p role="status">正在核对证券身份…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/>
    : candidates.length === 1 ? <HistoryChart key={candidates[0].id} instrument={candidates[0].id} period="日K"/>
      : <Empty title="尚未匹配唯一证券身份">日K只读取已存行情，不以名称猜测或临时补造走势。</Empty>;
}

export function AuctionVendor({ day, session, onLatestDay, onDayChange }: {
  day: string; session: SessionResponse; onLatestDay: (day: string) => void; onDayChange: (day: string) => void;
}) {
  const [family, setFamily] = useState<string>(VENDOR_FAMILIES[0]);
  const [query, setQuery] = useState(''), [direction, setDirection] = useState('降序');
  const [selectedCode, setCode] = useState(''), [tab, setTab] = useState('封单对比');
  const [auto, setAuto] = useState(true), [pages, setPages] = useState<Record<string, number>>({});
  const [optionsOpen, setOptionsOpen] = useState(false), optionsId = useId();
  const [notice, setNotice] = useState(''), [queuedAt, setQueuedAt] = useState<number | null>(null);
  const cache = useAuctionCache(day, auto);
  const batches = useMemo(() => allVendorBatches(cache.items), [cache.items]);
  const current = batches.filter(b => b.family === VENDOR_FAMILIES[0] && b.tradeDay && b.tradeDay <= today())
    .sort((a, b) => b.tradeDay!.localeCompare(a.tradeDay!) || Date.parse(b.receivedAt) - Date.parse(a.receivedAt))[0];
  const history = batches.filter(b => b.family === VENDOR_FAMILIES[1] && b.tradeDay && b.tradeDay <= today())
    .sort((a, b) => b.tradeDay!.localeCompare(a.tradeDay!) || Date.parse(b.receivedAt) - Date.parse(a.receivedAt));
  const historyDays = [...new Set(history.map(b => b.tradeDay!))].slice(0, 3);
  useEffect(() => { if (current?.tradeDay) onLatestDay(current.tradeDay); }, [current?.tradeDay, onLatestDay]);
  const batch = batches.filter(b => b.family === family && b.tradeDay === day).sort((a, b) => Date.parse(b.receivedAt) - Date.parse(a.receivedAt))[0];
  const currentRows = batch?.state === 'paired' ? sortStocks(batch.stocks, latestStage(batch), direction) : [];
  const stock = currentRows.find(s => s.code === selectedCode) ?? (!selectedCode ? currentRows[0] : undefined);
  const selected = stock?.code ?? selectedCode;
  const priorRead = usePreviousAuctionDay(day, cache.index);
  const previousDay = priorRead.busy || priorRead.error || priorRead.data?.day !== day ? null : priorRead.data.previous;
  const previousBatch = previousDay ? history.find(b => b.tradeDay === previousDay && b.state === 'paired') : undefined;
  const previousStock = previousBatch?.stocks.find(s => s.code === selected);
  const runtime = cache.index?.data.items.find(s => s.family === family);
  const failed = runtime?.last_attempt && ['error', 'unavailable'].includes(runtime.last_attempt.status);
  const revoked = runtime?.collection_state === 'permission_missing';
  const write = useSubmit(session.data.user.id, 'auction:collect');
  const blocked = !runtime ? '尚未配置来源' : !cache.index?.data.collection_enabled ? '本机采集总开关已关闭'
    : !runtime.provider.enabled ? '来源未启用' : runtime.provider.entitlement_status !== 'verified' ? '连接权益未核实'
      : runtime.provider.rights?.display !== 'allowed' || runtime.provider.rights?.cache !== 'allowed' ? '展示或缓存权限未核实'
        : runtime.credential_configured === false ? '本机凭据未配置' : runtime.paused_reason ? '来源已暂停' : '';
  useEffect(() => { setPages({}); }, [query, direction]);
  useEffect(() => {
    if (!queuedAt || !runtime?.last_attempt || Date.parse(runtime.last_attempt.started_at) < queuedAt) return;
    const a = runtime.last_attempt;
    setNotice(a.status === 'complete' ? '最近采集已成功，已读取新缓存。' : ['error', 'unavailable'].includes(a.status) ? `最近采集失败：${reasonText(a.reason) || '原因未提供'}，已有缓存保留。` : `最近采集状态：${a.status}；排队不代表成功。`);
  }, [runtime?.last_attempt, queuedAt]);
  async function collect() {
    if (!runtime || blocked) return;
    const started = Date.now();
    const result = await write.submit({ source: runtime.provider.id }, key => api.refreshSource(runtime.provider.id, session.data.csrf_token, key));
    if (result) { setQueuedAt(started); setNotice(`采集任务已排队（编号 ${result.data.job_id.slice(0, 8)}）；经 worker 执行，排队不代表成功。`); void cache.reload(); }
  }
  function choose(b: VendorBatch, code = '') { setFamily(b.family); onDayChange(b.tradeDay!); setCode(code); }
  const boards = [current, ...historyDays.filter(d => d !== current?.tradeDay).map(d => history.find(b => b.tradeDay === d))].filter((b): b is VendorBatch => !!b);
  if (batch && !boards.some(b => b.key === batch.key)) boards.push(batch);

  const detail = <section className="card auction-wb-detail" aria-label="个股详情">
      {stock && batch?.state === 'paired' ? <><div className="auction-wb-selected"><div><strong>{stock.name}</strong><span>{stock.code}</span>{[stock.tagsRaw, stock.boardRaw].filter(Boolean).map(t => <small key={t}>{t}</small>)}</div><span>{day} · {batch.tableKindLabel} · 已选标的</span><a href="#auction-notebook">记录当前标的观察 ↓</a></div><Segmented label="个股详情标签" options={['封单对比', '日K', '简介', '概念匹配', '异动', '资讯', '公告', '讨论', '来源截图']} value={tab} onChange={setTab}/>
        {tab === '封单对比' ? <div className="auction-wb-detail-body"><AuctionSealChart stock={stock} previous={previousStock} day={day} previousDay={previousDay ?? undefined}/></div>
          : tab === '日K' ? <StockHistory key={stock.code} stock={stock}/>
            : tab === '概念匹配' ? <div className="auction-wb-info"><h3>网站原标签</h3><p>{stock.tagsRaw ?? '来源未提供标签'} · {stock.boardRaw ?? '连板标签未知'}</p><p>标签只保留来源含义；主营与主题证据尚未关联。</p></div>
              : tab === '来源截图' ? <div className="auction-wb-info"><h3>短线侠截图参考</h3><p>本机尚无已归档截图；结构化取数失败时可查看原站人工核对。</p><a href="https://duanxianxia.com/web/jjlive" target="_blank" rel="noreferrer">打开短线侠公开页面</a><p>截图接入仍缺保存与时间登记；不把截图或拍摄时间当成结构化行情时点。</p></div>
                : <div className="auction-wb-info"><h3>{stock.name} · {tab}待联动</h3><p>{tab === '异动' ? '尚无可核验的原始异动说明，不根据概念标签生成原因。' : tab === '讨论' ? '第三方讨论未接入；个人观察与预案记录继续保存在下方。' : '该证券身份下的对应资料尚未关联，缺失保持未知。'}</p>{tab === '资讯' && <a href="/news">查看现有资讯来源 ›</a>}</div>}
        <details className="auction-wb-provenance"><summary>来源、日期与配对批次</summary><dl><div><dt>来源日期原文</dt><dd data-testid="auction-vendor-source-date">{batch.sourceDateRaw ?? '未知'}</dd></div><div><dt>表形态</dt><dd data-testid="auction-vendor-kind">{batch.tableKindLabel}</dd></div><div><dt>本机接收时间</dt><dd>{stamp(batch.receivedAt)}</dd></div><div><dt>当前批次</dt><dd>{batch.sourceName} · {batch.batchHash}</dd></div>{previousStock && previousBatch && <div><dt>对照批次</dt><dd>{previousBatch.tradeDay} · {previousBatch.sourceName} · {previousBatch.batchHash}</dd></div>}</dl><p>每个日期的 summary 与 stocks 同来源、同日期、同 batch_hash；各日分别保存，不跨批次拼值。</p></details>
      </> : <Empty title={revoked ? '该来源已撤回展示权限' : cache.busy && !batch ? '正在读取已存封单表…' : batch?.state === 'unpaired' ? '配对缺失，不展示明细' : selectedCode ? '该批次没有此标的' : '尚无已存封单表批次'}>从下方来源表选中标的；缺失不补零，截图与结构化数据分别核对。</Empty>}
    </section>;

  return <section className="auction-workbench auction-vendor" aria-label="竞价全景" aria-busy={cache.busy}>
    <div className="auction-wb-title"><h2>竞价全景</h2><div className="auction-wb-actions"><label className="auction-wb-search"><Icon name="search"/><input type="search" aria-label="搜索封单代码或名称或标签" placeholder="代码 / 名称 / 网站标签" value={query} maxLength={60} onChange={e => setQuery(e.target.value)}/></label><button type="button" onClick={() => void cache.reload()} disabled={cache.busy}><Icon name="refresh"/>刷新页面数据</button><button type="button" aria-expanded={optionsOpen} aria-controls={optionsId} onClick={() => setOptionsOpen(!optionsOpen)}>更多设置</button></div></div>
    <div className="auction-wb-summary-block"><div className="auction-wb-summary"><div className="auction-wb-source-controls"><label>来源日期 <input aria-label="封单来源日期" type="date" value={day} onChange={e => { if (e.target.value) { onDayChange(e.target.value); setCode(''); } }}/></label>{family === VENDOR_FAMILIES[1] && <label>历史交易日 <select aria-label="最近三个历史交易日" value={historyDays.includes(day) ? day : ''} onChange={e => { onDayChange(e.target.value); setCode(''); }}><option value="" disabled>选择历史交易日</option>{historyDays.map(d => <option value={d} key={d}>{d}（历史）</option>)}</select></label>}<Segmented label="封单排序方向" options={['降序', '升序']} value={direction} onChange={setDirection}/></div><div className="auction-wb-source-line"><span>最近可用日期：{current?.tradeDay ?? '尚无当前表'}</span>{runtime && <span data-testid="auction-vendor-collection">{sourceStates[runtime.collection_state] ?? runtime.collection_state}</span>}<span>封单金额 ≠ 成交额</span></div></div>
    <p className="feature-note auction-time-basis" role="status">网站阶段参考 · 非实时盘口 · 来源行情时点未知{batch && <> · 本批采集：<strong>{stamp(batch.receivedAt)}</strong></>}{batch && batch.pendingStages.length > 0 && <> · {batch.pendingStages.join('、')} 未采到，等待后续有效批次</>}。刷新页面读取已存缓存，不补造阶段值。</p></div>
    <div className="auction-wb-options" id={optionsId} hidden={!optionsOpen}>
      <div className="auction-wb-source-controls"><label>来源 <select aria-label="封单表来源" value={family} onChange={e => { setFamily(e.target.value); setCode(''); }}>{VENDOR_FAMILIES.map(f => <option value={f} key={f}>{VENDOR_FAMILY_LABEL[f]}</option>)}</select></label>{session.data.user.role === 'admin' && <button type="button" className="primary" disabled={write.busy || !!blocked} title={blocked || '提交到现有采集队列'} onClick={collect}>{write.busy ? '正在提交…' : '采集最新数据'}</button>}{session.data.user.role === 'admin' && blocked && <small>{blocked}</small>}</div>
      <div className="auction-wb-controls"><div><span className={`auction-auto-led ${auto ? 'on' : ''}`}/><span>{auto ? '自动读取缓存 · 65秒' : '自动读取已暂停'}</span><button type="button" className="auction-text-button" onClick={() => setAuto(!auto)}>{auto ? '暂停' : '恢复'}</button><small>最新已公布列排序 ↓</small></div><a href="/data?view=sources">查看来源状态 ›</a></div>
      <div className="auction-wb-source-line"><span>来源更新时间：未知</span><span>页面读取：{stamp(cache.received)}</span></div>
    <div className="auction-source-route"><span><b>竞价中</b>　免费公开阶段数据 / 短线侠</span><span><b>成交返回后</b>　Tushare 补充核对</span><span><b>失败时</b>　保留缓存 / 截图参考</span></div>
      <p className="feature-note">页面只读取缓存；网站阶段值不代表连续行情，封单变化不代表真实撤单。</p>
    </div>
    {notice && <p className="auction-notice" role="status">{notice}</p>}{write.error && <ErrorState error={write.error}/>}
    {cache.errors.length > 0 && <p role="status" className="auction-notice">缓存读取未全部成功（{cache.errors.map(f => VENDOR_FAMILY_LABEL[f] ?? f).join('、')}）；仅保留当前权限已核实的旧缓存，来源日期和采集时间保持原值。<button onClick={() => void cache.reload()}>重新读取</button></p>}
    {revoked && <p role="alert" className="auction-notice">该来源展示权限已撤回，历史封单表同样不再展示。</p>}
    {failed && <p role="status" className="auction-notice">最近一次采集失败（{reasonText(runtime?.last_attempt?.reason) || '原因未提供'}）；保留已存批次，非当前快照。</p>}
    <div className="auction-wb-main"><section className="card auction-wb-board" aria-label="多日封单对照"><div className="card-heading"><h2>多日封单对照</h2><small>最新已公布列排序 · 每日25条</small></div><div className="auction-wb-days" style={{ '--auction-day-count': Math.max(1, boards.length) } as CSSProperties}>{boards.length ? boards.map(b => {
      const stage = latestStage(b), rows = b.state === 'paired' ? sortStocks(b.stocks.filter(r => matchesStock(r, query)), stage, direction) : [];
      const totalPages = Math.max(1, Math.ceil(rows.length / AUCTION_PAGE_SIZE)), page = Math.min(pages[b.key] ?? 1, totalPages);
      return <section className={`auction-wb-day ${batch?.key === b.key ? 'active' : ''}`} key={b.key} aria-label={`${b.tradeDay} ${b.tableKindLabel}`}>
        <button type="button" className="auction-wb-date" onClick={() => choose(b)} aria-pressed={batch?.key === b.key} title={`${b.sourceName} · ${b.tableKindLabel} · ${b.tradeDay !== today() ? '历史日期' : '来源日期'}`}><strong>{b.tradeDay}</strong></button>
        {b.state === 'paired' ? <><div className="auction-wb-column-stages">{AUCTION_STAGES.map(s => <span key={s}>{s}</span>)}<span>涨幅</span></div><div className="auction-wb-stocks">{rows.slice((page - 1) * 25, page * 25).map(r => <button type="button" className={`auction-wb-stock ${selected === r.code ? 'linked' : ''}`} aria-label={`选择 ${b.tradeDay} ${r.code} ${r.name}`} title={`${r.name} ${r.code} · ${r.tagsRaw ?? ''}`} aria-pressed={selected === r.code && batch?.key === b.key} key={r.code} onClick={() => choose(b, r.code)}><span className="auction-wb-stock-label"><b>{r.name}</b>{[...new Set((r.tagsRaw ?? '').split(/[|｜,，、;；]/).map(t => t.trim()).filter(t => t && t !== r.boardRaw))].map(t => <span className="auction-wb-tag" key={t} title={r.tagsRaw ?? undefined}>{t}</span>)}{r.boardRaw && <span className="auction-wb-tag board">{r.boardRaw}</span>}</span><span className="auction-wb-amounts">{AUCTION_STAGES.map(s => <span key={s} title={`${s} ${r.amounts[s].cny ?? '未知'} 元 · 原文 ${r.amounts[s].raw ?? '未知'}`}>{compactMoney(r.amounts[s])}</span>)}<em className={r.changePct?.startsWith('-') ? 'negative' : r.changePct && Number(r.changePct) > 0 ? 'positive' : ''}>{r.changePctRaw ?? percent(r.changePct)}</em></span></button>)}</div>{!rows.length && <p className="feature-note">当前返回中无匹配标的，不推断为全市场零条。</p>}<div className="auction-wb-day-pager" role="group" aria-label={`${b.tradeDay}封单表分页`}><span>{rows.length}条 · {page}/{totalPages}页</span><button aria-label={`${b.tradeDay}上一页`} disabled={page <= 1} onClick={() => setPages(p => ({ ...p, [b.key]: page - 1 }))}>‹</button><button aria-label={`${b.tradeDay}下一页`} disabled={page >= totalPages} onClick={() => setPages(p => ({ ...p, [b.key]: page + 1 }))}>›</button></div><details className="auction-wb-day-meta"><summary>批次与采集状态</summary><div className="auction-wb-totals"><small>网站封单总额</small><div>{AUCTION_STAGES.map(s => <span key={s}><small>{s}</small><b>{compactMoney(b.summary!.totals[s])}</b></span>)}</div></div><p>本机接收 {stamp(b.receivedAt)}；{sourceStates[cache.index?.data.items.find(s => s.family === b.family)?.collection_state ?? ''] ?? '状态未知'}</p><p>{b.sourceName} · {b.batchHash}</p><p>来源标的 {b.summary?.rowCount ?? '未知'} 只 · 一字 {b.summary?.oneWordCount ?? '未知'} 只</p>{b.missingColumns.length > 0 && <p>缺少字段：{b.missingColumns.join('、')}</p>}</details></> : <p className="feature-note">配对缺失：{b.missing === 'summary' ? '缺汇总表' : '缺个股明细表'}；不展示合计，不补0，不重算供应商合计。</p>}
      </section>;
    }) : <Empty title={cache.busy ? '正在读取已存封单表…' : '尚无已存封单表批次'}>没有可用批次，去数据中心查看来源范围与状态。</Empty>}</div></section>
      <aside className="auction-wb-side" aria-label="竞价个股与观察">{detail}<AuctionObservationPool day={day} items={cache.items} batches={batch ? [batch] : []} sources={cache.index?.data.items ?? []} query={query} select={code => { setCode(code); setTab('封单对比'); }} previousDay={previousDay} calendarBusy={priorRead.busy} refresh={() => { void cache.reload(); priorRead.reload(); }} errors={cache.errors}/></aside>
    </div>
  </section>;
}
