import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';
import type { AuctionBatch, AuctionDetailRow, AuctionView, SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { useSubmit } from '../../shared/useSubmit';
import { Card, Empty, ErrorState, Segmented } from '../../shared/ui';
import { Icon } from '../../shared/icons';
import {
  AUCTION_OLD_AFTER_SECONDS as OLD_AFTER_SECONDS, AUCTION_PAGE_SIZE as PAGE_SIZE, AUCTION_STAGES as STAGES,
  ageText, batchStates, clock, count, percent, reasonText, shiftDay, sourceStates, stamp, today,
} from './auctionFormat';
import type { AuctionStage as Stage } from './auctionFormat';

const SORTS = ['来源顺序', '金额从高到低', '金额从低到高'] as const;

function amountValue(row: AuctionDetailRow) {
  if (row.amount_cny === null || row.amount_cny === '') return null;
  const number = Number(row.amount_cny);
  return Number.isFinite(number) ? number : null;
}
function isPaired(batch: AuctionBatch) {
  return batch.state !== 'unpaired' && batch.detail.length > 0;
}
function isOld(batch: AuctionBatch) {
  return batch.state === 'stale' || batch.age_seconds > OLD_AFTER_SECONDS;
}

/** The one batch a stage may read: same source, same day, same stored batch. */
function batchFor(stage: Stage, items: AuctionBatch[], family: string, day: string) {
  return items.find(item => item.family === family && item.trade_day === day && item.checkpoint === stage);
}

function StateBadge({ batch }: { batch: AuctionBatch }) {
  return <span className={batch.state === 'available' ? 'badge auction-state' : 'badge muted auction-state'} data-testid={`auction-stage-${batch.checkpoint ?? 'unknown'}-state`}>{batchStates[batch.state]}</span>;
}

function StageRows({ items, family, day }: { items: AuctionBatch[]; family: string; day: string }) {
  const unknownTime = items.filter(item => item.family === family && item.trade_day === day && !item.checkpoint).length;
  return <>
    <div className="table-wrap auction-stage-wrap" tabIndex={0} role="region" aria-label="三阶段封单汇总表格">
      <table className="auction-stage-table">
        <thead><tr>
          {['检查点', '来源日期', '本机采集时间', '有效报价', '竞价量（股）', '竞价额（成交额，元）', '涨 / 跌 / 平', '封单金额'].map(label =>
            <th scope="col" key={label}>{label}</th>)}
        </tr></thead>
        <tbody>{STAGES.map(stage => {
          const batch = batchFor(stage, items, family, day);
          if (!batch) {
            return <tr key={stage} className="auction-stage-empty">
              <th scope="row">{stage}</th>
              <td colSpan={7}><span className="auction-missing">该检查点尚无已配对批次；不补 0、不推算合计。</span></td>
            </tr>;
          }
          const summary = isPaired(batch) ? batch.summary : null;
          return <Fragment key={stage}>
            <tr className={isOld(batch) ? 'auction-stage-old' : undefined}>
              <th scope="row"><span className="auction-stage-name">{stage}</span><StateBadge batch={batch}/></th>
              <td><span>{batch.trade_day ?? '日期未知'}</span><small>{batch.trade_day_basis === 'source_time' ? '来源报价日期' : '按接收时间归日'}</small></td>
              <td><span>{stamp(batch.received_at)}</span><small>{ageText(batch.age_seconds)}{isOld(batch) ? ' · 非当前快照' : ''}</small></td>
              <td>{summary ? <>{summary.quoted_count} / {summary.instrument_count}{summary.rejected_count > 0 && <small>未纳入 {summary.rejected_count}</small>}</> : '—'}</td>
              <td>{summary ? count(summary.total_volume_shares) : '—'}</td>
              <td>{summary ? count(summary.total_amount_cny) : '—'}</td>
              <td>{summary ? `涨 ${summary.up_count} · 跌 ${summary.down_count} · 平 ${summary.flat_count}` : '—'}</td>
              <td><span className="badge muted" title="公开盘前快照不含委托队列与未匹配量；竞价额是成交额，不能作为封单金额">未接入</span></td>
            </tr>
            {batch.note && <tr className="auction-stage-note"><td/><td colSpan={7}><span className="auction-missing">{batch.note}</span></td></tr>}
          </Fragment>;
        })}</tbody>
      </table>
    </div>
    {unknownTime > 0 && <p className="auction-missing" role="status">另有 {unknownTime} 个批次来源时点未命中 09:15/09:20/09:25 ±60 秒，检查点未知，未并入三阶段视图。</p>}
  </>;
}

function Compare({ code, items, family, day, name }: { code: string; items: AuctionBatch[]; family: string; day: string; name: string }) {
  const stages = STAGES.map(stage => {
    const batch = batchFor(stage, items, family, day);
    return { stage, batch, row: batch?.detail.find(item => item.code === code) };
  });
  const fields: Array<[string, (row: AuctionDetailRow) => string]> = [
    ['竞价价（元）', row => row.price_cny ?? '—'],
    ['昨收（元）', row => row.prev_close_cny ?? '—'],
    ['涨跌幅', row => percent(row.change_pct)],
    ['竞价量（股）', row => count(row.volume_shares)],
    ['竞价额（元）', row => count(row.amount_cny)],
    ['来源时点', row => clock(row.quote_time)],
  ];
  return <>
    <div className="auction-compare-head"><strong>{code} {name}</strong><small>{stages.find(item => item.batch)?.batch?.source_name ?? '来源未知'} · {day}</small></div>
    <div className="table-wrap auction-compare-wrap" tabIndex={0} role="region" aria-label="个股三阶段对比表格">
      <table className="auction-compare-table">
        <thead><tr><th scope="col">字段</th>{STAGES.map(stage => <th scope="col" key={stage}>{stage}</th>)}</tr></thead>
        <tbody>
          {fields.map(([label, read]) => <tr key={label}>
            <th scope="row">{label}</th>
            {stages.map(({ stage, batch, row }) => <td key={stage}>{row ? read(row) : <span className="auction-missing">{batch ? '批次无此标的' : '无该时点批次'}</span>}</td>)}
          </tr>)}
          <tr><th scope="row">本机采集时间</th>{stages.map(({ stage, batch }) => <td key={stage}>{batch ? stamp(batch.received_at) : <span className="auction-missing">无该时点批次</span>}</td>)}</tr>
          <tr><th scope="row">批次状态</th>{stages.map(({ stage, batch }) => <td key={stage}>{batch ? <StateBadge batch={batch}/> : <span className="auction-missing">无该时点批次</span>}</td>)}</tr>
        </tbody>
      </table>
    </div>
    <p className="feature-note">同一来源、同一交易日的三份已配对批次并列；缺失时点保持“无该时点批次”，不补造价格、成交量或封单。此处不提供分时图，也不推断封单、撤单与趋势。</p>
  </>;
}

export function AuctionMarket({ session, day, onDayChange }: { session: SessionResponse; day: string; onDayChange: (day: string) => void }) {
  const [family, setFamily] = useState('');
  const [stage, setStage] = useState<Stage | ''>('');
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<string>(SORTS[1]);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<{ code: string; name: string } | null>(null);
  const [notice, setNotice] = useState('');

  const load = useCallback(() => api.marketAuction(day), [day]);
  const read = useRead(load);
  const view: AuctionView | undefined = !read.busy && !read.error ? read.data?.data : undefined;
  const items = view?.items ?? [];
  const sources = view?.sources ?? [];
  const active = family || sources.find(row => items.some(item => item.family === row.family && item.trade_day === day))?.family || sources[0]?.family || '';
  const sourceName = sources.find(row => row.family === active)?.source_name ?? '来源未配置';
  const dayItems = items.filter(item => item.family === active && item.trade_day === day);
  const unknownDay = items.filter(item => item.family === active && !item.trade_day).length;
  const latest = [...dayItems].sort((left, right) => Date.parse(right.received_at) - Date.parse(left.received_at))[0];
  const activeStage: Stage | '' = stage || ([...STAGES].reverse().find(item => batchFor(item, items, active, day)) ?? '');
  const batch = activeStage ? batchFor(activeStage, items, active, day) : undefined;

  const admin = session.data.user.role === 'admin';
  const index = useRead(useCallback(() => admin ? api.dataSources() : Promise.resolve(null), [admin]));
  const runtime = index.data?.data.items.find(row => row.family === active);
  const collectionEnabled = index.data?.data.collection_enabled;
  const write = useSubmit(session.data.user.id, 'auction:collect');

  useEffect(() => { setPage(1); }, [query, sort, activeStage, day, active]);
  useEffect(() => { setSelected(null); }, [day, active]);

  const rows = useMemo(() => {
    const value = batch?.detail ?? [];
    const text = query.trim().toLowerCase();
    const matched = text ? value.filter(row => row.code.toLowerCase().includes(text) || row.name.toLowerCase().includes(text)) : value;
    if (sort === '来源顺序') return matched;
    const direction = sort === '金额从高到低' ? -1 : 1;
    return [...matched].sort((left, right) => {
      const a = amountValue(left); const b = amountValue(right);
      if (a === null && b === null) return 0;
      if (a === null) return 1;
      if (b === null) return -1;
      return (a - b) * direction;
    });
  }, [batch, query, sort]);

  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const current = Math.min(page, pages);
  const visible = rows.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE);
  const from = rows.length ? (current - 1) * PAGE_SIZE + 1 : 0;
  const to = rows.length ? from + visible.length - 1 : 0;

  function blockReason() {
    if (!runtime) return '来源尚未配置，请先在数据中心配置';
    if (!collectionEnabled) return '本机采集总开关已关闭';
    if (!runtime.provider.enabled) return '来源未启用';
    if (runtime.provider.entitlement_status !== 'verified') return '连接权益未核实';
    if (runtime.credential_configured === false) return '本机凭据未配置';
    if (runtime.paused_reason) return '来源已暂停';
    return '';
  }
  const blocked = blockReason();

  async function collect() {
    if (!runtime || blocked) return;
    const result = await write.submit({ source: runtime.provider.id }, key => api.refreshSource(runtime.provider.id, session.data.csrf_token, key));
    if (result) {
      setNotice(`采集任务已排队（编号 ${result.data.job_id.slice(0, 8)}）。排队不代表采集成功；任务完成后请点“重新读取”查看实际批次。`);
      index.reload();
    }
  }

  const sourceOptions = sources.map(row => row.source_name);

  return <>
    <div className="auction-toolbar">
      <div className="auction-day">
        <label>竞价交易日<input type="date" aria-label="竞价交易日" value={day} max={today()} onChange={event => { onDayChange(event.target.value); setNotice(''); }}/></label>
        <button type="button" aria-label="前一自然日" onClick={() => onDayChange(shiftDay(day, -1))}>‹ 前一日</button>
        <button type="button" aria-label="后一自然日" disabled={day >= today()} onClick={() => onDayChange(shiftDay(day, 1))}>后一日 ›</button>
      </div>
      <div className="auction-clock" role="status">
        <Icon name="auction"/>
        <strong>{latest ? `${isPaired(latest) ? '最近已配对批次' : '最近批次配对缺失'} ${latest.checkpoint ?? '时点未知'} · ${ageText(latest.age_seconds)}` : `${day} 尚无已存批次`}</strong>
        <small>{latest ? `本机采集 ${stamp(latest.received_at)} · ${latest.source_name}` : '可切换日期，或由管理员在来源就绪后采集一次'}</small>
      </div>
    </div>

    <Card title="三阶段封单汇总" className="auction-stages" action={<button type="button" onClick={read.reload} disabled={read.busy}><Icon name="refresh"/>重新读取</button>}>
      <div className="auction-filters">
        {sourceOptions.length > 0 && <Segmented label="竞价来源" options={sourceOptions} value={sourceName} onChange={name => { setFamily(sources[sourceOptions.indexOf(name)]?.family ?? ''); setNotice(''); }}/>}
        {admin && <button type="button" className="primary" disabled={write.busy || !!blocked} onClick={collect} title={blocked || '提交一次采集；排队不等于成功'}>{write.busy ? '正在提交…' : '采集一次'}</button>}
      </div>
      {admin && blocked && <p className="feature-note">{blocked}；可在数据中心处理后重试。</p>}
      {!admin && <p className="feature-note">当前账号可读取已存批次；采集由管理员在数据中心执行。</p>}
      {notice && <p className="auction-notice" role="status">{notice}</p>}
      {write.error && <ErrorState error={write.error}/>}
      <p className="feature-note">封单金额未接入：公开盘前快照只有竞价价、量与额，没有委托队列或未匹配量。表内“竞价额”是成交额，不能当作封单金额。</p>
      {day !== today() && <p className="auction-notice" role="note">正在查看历史交易日 {day}：显示当日已存批次，不是当前快照。</p>}
      {unknownDay > 0 && <p className="auction-missing" role="status">另有 {unknownDay} 个批次交易日未知，未并入当日视图。</p>}
      {read.busy ? <div className="resource-loading" role="status">正在读取已存竞价批次…</div>
        : read.error ? <ErrorState error={read.error} retry={read.reload}/>
        : !view ? null
        : <>
          <div className="auction-sources" aria-label="竞价来源状态">{sources.map(row =>
            <div key={row.family} data-testid={`auction-source-${row.family}`}>
              <strong>{row.source_name}</strong>
              <span className={row.state === 'available' ? 'badge' : 'badge muted'}>{sourceStates[row.state] ?? row.state}</span>
              <small>{row.reason ? reasonText(row.reason) : `最近成功 ${stamp(row.last_success_at)}`}</small>
            </div>)}
          </div>
          {sources.some(row => row.state === 'permission_missing') &&
            <p className="auction-note" role="alert"><Icon name="shield"/><span>来源展示权限已撤回，历史批次同样不再返回；数据未删除，恢复权利后可再读。</span></p>}
          {dayItems.length || unknownDay ? <StageRows items={items} family={active} day={day}/>
            : <Empty title="尚无已存竞价批次">{day} 没有该来源的已配对批次。{sources.some(row => row.state === 'empty') ? '来源尚未配置或尚未采集，可在数据中心配置后采集一次。' : '可切换交易日或来源查看已存批次；不会用收盘价或分钟线补造检查点。'}</Empty>}
        </>}
    </Card>

    <div className="auction-market">
      <Card title="个股明细" className="auction-detail-table" action={<span className="badge muted">每页 {PAGE_SIZE} 条</span>}>
        <div className="auction-table-controls">
          <label className="auction-search"><Icon name="search"/><input aria-label="搜索代码或名称" type="search" value={query} maxLength={40} placeholder="代码或名称" onChange={event => setQuery(event.target.value)}/></label>
          <Segmented label="明细检查点" options={STAGES} value={activeStage || STAGES[0]} onChange={value => setStage(value as Stage)}/>
          <Segmented label="竞价额排序" options={SORTS} value={sort} onChange={setSort}/>
        </div>
        <p className="feature-note">{batch ? <>只读取 {sourceName} · {day} · {activeStage} 的同一批次（{batch.batch_hash.slice(0, 16)}…，接收 {stamp(batch.received_at)}）；搜索、排序与分页都在本批次内进行。</> : <>选择检查点后读取该时点的已配对批次；没有批次时不展示 0 行或推算数据。</>}</p>
        <div className="table-wrap" tabIndex={0} role="region" aria-label="个股竞价明细表格">
          <table className="auction-rows">
            <thead><tr>{['代码', '名称', '竞价价（元）', '涨跌幅', '竞价量（股）', '竞价额（元）', '来源时点', '对比'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
            <tbody>
              {visible.map(row => <tr key={row.code} className={selected?.code === row.code ? 'selected' : undefined}>
                <td>{row.code}</td><td>{row.name}</td><td>{row.price_cny ?? '—'}</td><td>{percent(row.change_pct)}</td>
                <td>{count(row.volume_shares)}</td><td>{count(row.amount_cny)}</td><td>{clock(row.quote_time)}</td>
                <td><button type="button" aria-pressed={selected?.code === row.code} aria-label={`对比 ${row.code} ${row.name}`} onClick={() => setSelected({ code: row.code, name: row.name })}>对比</button></td>
              </tr>)}
              {!visible.length && <tr><td colSpan={8}>
                {read.busy ? <Empty title="正在读取已存批次">只展示同一来源、同一交易日的已配对批次。</Empty>
                  : !batch ? <Empty title="该检查点尚无已配对批次">汇总与明细必须来自同一批次；缺失时不展示推算行或合计。</Empty>
                  : !batch.detail.length ? <Empty title="该批次没有可展示的明细">未配对批次不补 0 行、不推算合计。</Empty>
                  : <Empty title="没有匹配的标的">当前批次内没有代码或名称匹配“{query}”；清空搜索可查看全部 {batch.detail.length} 条。</Empty>}
              </td></tr>}
            </tbody>
          </table>
        </div>
        <div className="auction-pager" role="group" aria-label="明细分页">
          <span>{`第 ${from}–${to} 条 / 共 ${rows.length} 条 · 每页 ${PAGE_SIZE} 条`}</span>
          <span>{`第 ${current} / ${pages} 页`}</span>
          <button type="button" disabled={current <= 1} onClick={() => setPage(current - 1)}>上一页</button>
          <button type="button" disabled={current >= pages} onClick={() => setPage(current + 1)}>下一页</button>
        </div>
        {batch && <p className="auction-batch-foot">批次 {batch.batch_hash.slice(0, 16)}… · 时点依据：{batch.checkpoint_basis === 'source_time' ? '来源时间' : '未知（接收时间不冒充行情时点）'} · 竞价量为股、竞价额为元</p>}
        {batch?.note && <p className="auction-note" role="status"><Icon name="info"/><span>{batch.note}</span></p>}
      </Card>

      <Card title="个股三阶段对比" className="auction-compare" action={selected ? <button type="button" onClick={() => setSelected(null)}>清除选择</button> : undefined}>
        {selected
          ? <Compare code={selected.code} name={selected.name} items={items} family={active} day={day}/>
          : <Empty title="尚未选择标的">在个股明细中点击“对比”，查看同一来源、同一交易日三个检查点的已存数值。</Empty>}
      </Card>
    </div>

    <div className="research-status-note"><Icon name="info"/><span>竞价批次来自本机已存缓存：封单金额、撤单与委托队列未接入，竞价额是成交额。竞价监控与交易执行未启用。</span></div>
  </>;
}
