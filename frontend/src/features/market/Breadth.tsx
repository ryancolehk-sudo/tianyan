import { useCallback, useEffect } from 'react';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { today } from './auctionFormat';

const dataset = 'market.ashare.breadth';
const fields = [['limit_up', '涨停'], ['limit_down', '跌停'], ['broken_limit', '炸板'], ['advancing', '上涨'], ['declining', '下跌']];

export function MarketBreadth({ compact = false, workbench = false, day = '' }: { compact?: boolean; workbench?: boolean; day?: string }) {
  const read = useRead(useCallback(async () => {
    const [breadth, pool, paid] = await Promise.allSettled([api.datasets(dataset, undefined, day || undefined), compact ? api.datasets('tushare.limit_list_d', 'tushare_limit_list_d', day || undefined) : Promise.resolve(null), day ? api.breadthHistory(day) : api.breadthLatest()]);
    if (breadth.status === 'rejected' && !(paid.status === 'fulfilled' && ['tushare_market_rt','tencent_market_rt'].includes(paid.value.data.source_family ?? '') && paid.value.data.latest)) throw breadth.reason;
    return { requestDay: day, ...(breadth.status === 'fulfilled' ? breadth.value : { data: { items: [] } }), pool: pool.status === 'fulfilled' ? pool.value : null, paid: paid.status === 'fulfilled' ? paid.value : null };
  }, [compact, day]));
  useEffect(() => {
    const refresh = () => { if (document.visibilityState === 'visible') read.reload(); };
    const timer = window.setInterval(refresh, 60_000);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [read.reload]);
  const current = !read.error && read.data?.requestDay === day ? read.data : undefined;
  const record = current?.data.items.find(item => item.family === 'ashare_breadth' && item.payload.dataset === dataset);
  const candidate = current?.paid?.data;
  const paid = candidate && ['tushare_market_rt','tencent_market_rt'].includes(candidate.source_family ?? '') && (!day || candidate.observation_day === day) ? candidate : undefined;
  const sample = paid?.latest;
  const supplier = paid?.source_family === 'tencent_market_rt' ? '腾讯' : 'Tushare';
  const row = sample ? Object.fromEntries(['limit_up','limit_down','advancing','declining'].map(key => [key, sample[key as 'limit_up' | 'limit_down' | 'advancing' | 'declining']?.toString() ?? null])) : record?.payload.rows[0];
  const provenance = paid ? `${supplier} · 当日限制价精确比较 · 返回 ${sample?.returned ?? '—'} / 可比 ${sample?.comparable ?? '—'} / 未判定 ${sample?.unknown ?? '—'}（旧日 ${sample?.old_day ?? '—'}）；${paid.scope}` : undefined;
  const details = provenance ?? (record ? `来源：${record.source_name}；${record.payload.scope} 服务器首次接收：${new Date(record.received_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}。采集时点不是行情时点。` : '无数据保持未知，不将原型数字或缺失字段补成零。');
  if (workbench) {
    const display = [['advancing', '上涨', 'up'], ['declining', '下跌', 'down'], ['limit_up', '涨停', 'up'], ['limit_down', '跌停', 'down'], ['broken_limit', '炸板', 'neutral']];
    return <div className="workbench-breadth" aria-label="市场广度参考" title={details}><div>{display.map(([key, name, tone]) => <span className={'signal-' + tone} key={key}>{name}<strong data-testid={'breadth-' + key}>{row?.[key] != null && /^\d+$/.test(row[key]!) ? row[key] : '—'}</strong></span>)}{sample?.total_amount_cny != null && <span>样本成交额<strong>{(Number(sample.total_amount_cny) / 1e8).toFixed(2)}亿</strong></span>}</div><small>{read.error ? <>广度读取失败 <button type="button" onClick={read.reload}>重试</button></> : paid ? paid.observation_day + ' · ' + supplier + ' · 可比 ' + (sample?.comparable ?? '—') + ' / 未判定 ' + (sample?.unknown ?? '—') : record ? (record.payload.observed_day ?? '源日期未知') + ' · 供应商广度' : read.busy ? '正在读取广度…' : '暂无广度数据'}</small></div>;
  }
  if (compact) {
    const count = (key: string) => typeof row?.[key] === 'string' && /^\d+$/.test(row[key]!) ? row[key]! : null;
    const up = count('limit_up'), broken = count('broken_limit');
    const seal = record?.payload.observed_day === today() && up !== null && broken !== null && Number(up) + Number(broken) > 0
      ? (Number(up) / (Number(up) + Number(broken)) * 100).toFixed(1) + '%' : null;
    const pool = read.data?.pool?.data.items.filter(i => i.family === 'tushare_limit_list_d' && i.payload.dataset === 'tushare.limit_list_d')
      .sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at))[0];
    const rows = !read.error ? pool?.payload.rows ?? [] : [];
    const winners = rows.filter(r => r.limit === 'U');
    const validPool = !!rows.length && /^\d{8}$/.test(rows[0].trade_date ?? '')
      && rows.every(r => r.trade_date === rows[0].trade_date && /^\d{6}\.(SH|SZ|BJ)$/.test(r.ts_code ?? ''))
      && new Set(rows.map(r => r.ts_code)).size === rows.length;
    const height = validPool && winners.length && winners.every(r => /^\d+$/.test(r.limit_times ?? ''))
      ? String(Math.max(...winners.map(r => Number(r.limit_times)))) : null;
    const primary = [
      ['sentiment', '情绪指标', null, 'empty'], ['limit_up', '涨停家数', up, 'up'],
      ['limit_down', '跌停家数', count('limit_down'), 'down'], ['broken_limit', '炸板数量', broken, 'neutral'],
      ['loss_effect', '亏钱效应', null, 'empty'],
    ];
    const secondary = [
      ['main_flow', '主力流入', null, 'empty'], ['height', '连板高度', height, 'neutral'],
      ['advancing', '上涨家数', count('advancing'), 'up'], ['declining', '下跌家数', count('declining'), 'down'],
      ['seal_ratio', '今日封板率', seal, 'neutral'], ['previous_limit', '昨涨停表现', null, 'empty'],
      ['previous_streak', '昨连板表现', null, 'empty'], ['five_minute', '沪深5分钟量能', null, 'empty'],
    ];
    const cells = (items: (string | null)[][]) => items.map(([key, label, value, tone]) => <div className={`signal-cell signal-${tone}`} key={key}
      title={key === 'height' ? `供应商返回范围内最高连板；原表日期 ${rows[0]?.trade_date ?? '未知'}，不是全市场完整保证。` : key === 'seal_ratio' ? '同一广度批次：涨停 /（涨停 + 炸板）；必要字段缺失时不计算。' : value === null ? '暂无可核验数据，保持空缺。' : details}>
      <span>{label}：</span><strong data-testid={'breadth-' + key}>{value ?? '—'}</strong></div>);
    return <div className="auction-signals" aria-label="市场广度参考" title={details}>
      <div className="signal-primary">{cells(primary)}</div><div className="signal-secondary">{cells(secondary)}</div>
      <small>{read.error ? <>广度读取失败 <button type="button" className="breadth-retry" onClick={read.reload}>重试</button></> : <a href="/data?view=datasets">{paid ? `${supplier} · 可比 ${sample?.comparable ?? '—'} / 未判定 ${sample?.unknown ?? '—'}` : record ? '免费备用参考 · 源日期未知' : read.busy ? '正在读取市场广度…' : '市场广度未接入'} · 连板仅返回范围</a>}</small>
    </div>;
  }
  return <div className="breadth-tile" aria-label="市场广度参考" title={details}>
    {fields.map(([key, label]) => <div key={key}><span>{label}</span><strong data-testid={'breadth-' + key}>{row?.[key] !== null && row?.[key] !== undefined && /^\d+$/.test(row[key]!) ? row[key] : '—'}</strong></div>)}
    <small>{read.error ? <>广度读取失败 <button type="button" className="breadth-retry" onClick={read.reload}>重试</button></> : paid ? <a href="/data?view=datasets">{supplier} · 可比 {sample?.comparable ?? '—'} / 未判定 {sample?.unknown ?? '—'}</a> : record ? <a href="/data?view=datasets">免费备用参考 · 源日期未知</a> : read.busy ? '正在读取市场广度…' : '市场广度未接入'}</small>
  </div>;
}
