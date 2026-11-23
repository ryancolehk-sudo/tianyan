import { useState } from 'react';
import { HistoryPlot } from '../../shared/HistoryPlot';
import { api } from '../../shared/api';
import { reviewLookback, usePreferences } from '../../shared/preferences';
import { useBoundRead, Failure, Loading } from './components';
import type { ReviewProps } from './context';
import { number, pct, text } from './model';

const groups = [['one_to_two', '一进二'], ['two_to_three', '二进三'], ['three_to_four', '三进四'], ['four_plus', '四板以上']] as const;
export function EmotionHistory({ series, day, asOf, select }: ReviewProps) {
  const chart = new URLSearchParams(location.search).get('emotion_chart');
  const { preferences } = usePreferences();
  const [kind, setKind] = useState(chart && ['limits','broken','height','performance','promotion'].includes(chart) ? chart : 'limits');
  const query = new URLSearchParams(location.search);
  const params = new URLSearchParams({ kind: 'emotion', trade_day: query.get('range_end') || series?.trade_days.at(-1) || day, lookback: reviewLookback(query, preferences), ...(asOf ? { as_of_time: asOf } : {}) });
  const read = useBoundRead(`emotion-history:${params}:${!!chart}`, () => chart ? api.reviewAnalysis(params) : Promise.resolve(null));
  const selected = read.data?.data.records.find(r => r.trade_day === day)?.metrics;
  const choices = [['limits', '涨跌停'], ['broken', '炸板率'], ['height', '最高／次高板'], ['performance', '昨日首板／连板表现'], ['promotion', '梯队晋级率']];
  const labels = kind === 'limits' ? ['涨停', '跌停'] : kind === 'broken' ? ['炸板率'] : kind === 'height' ? ['最高板', '次高板（不同高度）'] : kind === 'performance' ? ['昨日首板收盘均值', '昨日连板收盘均值'] : groups.map(g => g[1]);
  const incompletePerformanceDays = kind === 'performance' ? (read.data?.data.records ?? [])
    .filter(r => (r.metrics?.relay?.unclassified_previous_limit_up_n ?? 0) > 0).map(r => r.trade_day) : [];
  const points = (read.data?.data.records ?? []).map(r => {
    const pool = r.metrics?.limits?.supplier_reported, relay = r.metrics?.relay;
    const incompletePerformance = kind === 'performance' && (relay?.unclassified_previous_limit_up_n ?? 0) > 0;
    const values = kind === 'limits' ? [pool?.up, pool?.down] : kind === 'broken' ? [pool?.broken_rate_pct] : kind === 'height' ? [pool?.height_missing_n ? null : pool?.max_height, pool?.height_missing_n ? null : pool?.second_height] : kind === 'performance' ? incompletePerformance ? [null, null] : [relay?.first_board?.close_mean_pct, relay?.consecutive?.close_mean_pct] : groups.map(([key]) => relay?.by_height?.[key]?.promotion_pct);
    return { day: r.trade_day, values: values.map(number), note: incompletePerformance ? '昨日原板数缺失，首板／连板分类不足' : r.state === 'complete' ? '' : '归档资料不足' };
  });
  return <details className="review-extension" open={!!chart} onToggle={e=>{if(e.currentTarget.open!==!!chart)select({emotion_chart:e.currentTarget.open?kind:null});}}><summary>历史曲线与梯队晋级率</summary><div className="review-extension-body"><div className="review-extension-tools">{choices.map(([key, label]) => <button key={key} aria-pressed={key === kind} onClick={() => {setKind(key);select({emotion_chart:key});}}>{label}</button>)}</div>
    <Failure error={read.error} retry={read.reload}/>{read.busy && !read.data && <Loading/>}
    <HistoryPlot points={points} labels={labels} selected={day} select={d => select({ trade_day: d, record_key: null, range_end: read.data?.data.trade_days.at(-1) ?? day, emotion_chart: kind })} unit={kind === 'height' ? '板' : kind === 'limits' ? '只' : '%'}/>
    {!!incompletePerformanceDays.length && <p className="history-plot-note" role="status">昨日原板数缺失，首板／连板样本无法完整分类，两组表现曲线留空：{incompletePerformanceDays.join('、')}。</p>}
    <div className="review-extension-scroll"><table aria-label="所选日梯队晋级样本"><thead><tr><th>{selected?.relay?.previous_trade_day}固定样本</th><th>样本数</th><th>有效核验</th><th>晋级数</th><th>晋级率</th><th>查看样本</th></tr></thead><tbody>{groups.map(([key, label]) => { const g = selected?.relay?.by_height?.[key]; return <tr key={key}><td>{label}</td><td>{text(g?.N)}</td><td>{text(g?.promotion_valid_n)}</td><td>{text(g?.promotion_n)}</td><td>{pct(g?.promotion_pct, false)}</td><td><details><summary>{label}样本</summary>{g?.samples?.length ? g.samples.map(s => <button key={String(s.ts_code)} onClick={() => select({ stock: String(s.ts_code), view: 'direction' })}>{text(s.ts_code)} · {s.promotion === true ? '封板' : s.promotion === false ? '未封板' : '待核'} </button>) : '没有已返回样本'}</details></td></tr>; })}</tbody></table></div>
    <p className="history-plot-note">与多日梯队统一使用已购服务的原日涨停池；日频归档未齐时留空。晋级率保留昨日固定分母，缺少报价、限制价或昨日高度时留空。</p>
  </div></details>;
}
