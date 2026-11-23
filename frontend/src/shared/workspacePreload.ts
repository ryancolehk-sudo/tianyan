import { api } from './api';
import { dataCenterApi } from '../features/data-center-live/api';
import { newsApi } from '../features/news-center/newsApi';
import { loadWorkbench } from '../features/terminal/data';
import { terminalTabs, terminalView } from './terminalNavigation';
import { prepareTerminalDetails, startPreparedDetails } from '../features/terminal/details';
import { blankNotes, number } from '../features/daily-review/model';
import type { Day, NotesContent, Series } from '../features/daily-review/model';
import { defaultPreferences, newsCategory, reviewLookback } from './preferences';
import { ApiError } from './api';
import type { Preferences } from './preferences';

export type PreparationStep = { label: string; state: 'waiting' | 'loading' | 'ready' | 'failed'; error?: string };
type Task = PreparationStep & { run: () => Promise<void> };

export class WorkspacePreparation {
  private listeners = new Set<() => void>();
  private stopped = false;
  private running = false;
  private started = false;
  private tasks: Task[];
  snapshot: PreparationStep[] = [];

  constructor(initial: URL, admin: boolean, preferences: Preferences = defaultPreferences) {
    const q = initial.searchParams;
    const reviewHere = initial.pathname === '/research/reviews';
    const reviewQuery = new URLSearchParams(reviewHere ? [...q].filter(([key]) => ['trade_day', 'as_of_time'].includes(key)) : []);
    const seriesQuery = new URLSearchParams({ lookback: reviewLookback(reviewHere ? q : new URLSearchParams(), preferences) });
    if (reviewHere && q.get('range_end')) seriesQuery.set('trade_day', q.get('range_end')!);
    if (reviewQuery.has('as_of_time')) seriesQuery.set('as_of_time', reviewQuery.get('as_of_time')!);
    let day: Day | undefined;
    let series: Series | undefined;
    let notes: NotesContent = blankNotes();
    const read = async <T,>(load: () => Promise<T>) => {
      if (this.stopped) throw new Error('准备已停止。');
      return load();
    };
    this.tasks = [
      { label: '每日复盘 · 同日行情与笔记', state: 'waiting', run: async () => {
        // Warm the saved reads without downloading an unopened page's UI.
        // Its route loads on demand and reuses these same cached responses.
        day = (await read(() => api.reviewDay(reviewQuery))).data;
        await read(api.watchlist);
        if (day.shared_date) {
          const params = new URLSearchParams(reviewQuery); params.set('trade_day', day.shared_date);
          notes = (await read(() => api.reviewNotes(params))).data.content;
          await read(() => api.reviewNoteDays(new URLSearchParams({ ...Object.fromEntries(params), lookback: '60' })));
          await read(() => api.reviewLhb(params));
          const sector = (reviewHere ? q.get('sector') : '') || notes.directions[0]?.sector_code
            || [...day.themes.items].sort((a, b) => (number(b.provider_record.pct_change) ?? -Infinity) - (number(a.provider_record.pct_change) ?? -Infinity))[0]?.ts_code;
          if (sector) await read(() => api.reviewDay(new URLSearchParams({ ...Object.fromEntries(params), sector })));
          const codes = [...new Set([...notes.directions.map(item => item.sector_code), ...(sector ? [sector] : [])])].slice(0, 10);
          if (codes.length) await read(() => api.reviewThemes(new URLSearchParams({ ...Object.fromEntries(params), sector_codes: codes.join(',') })));
          if (notes.core_stocks.length) await read(() => api.reviewStocks(new URLSearchParams({ ...Object.fromEntries(params), codes: notes.core_stocks.map(item => item.code).join(',') })));
          if (reviewHere && q.get('stock') && !notes.core_stocks.some(item => item.code === q.get('stock'))) {
            await read(() => api.reviewStocks(new URLSearchParams({ ...Object.fromEntries(params), codes: q.get('stock')! })));
          }
        }
      } },
      { label: '情绪周期 · 近期交易日', state: 'waiting', run: async () => {
        seriesQuery.set('lookback', reviewLookback(reviewHere ? q : new URLSearchParams(), preferences));
        series = (await read(() => api.reviewSeries(seriesQuery))).data;
        const anchor = series.trade_days.at(-1);
        if (anchor) await read(() => api.reviewNoteDays(new URLSearchParams({ trade_day: anchor, lookback: '60', ...(reviewQuery.has('as_of_time') ? { as_of_time: reviewQuery.get('as_of_time')! } : {}) })));
      } },
      { label: '工作台 · 四个观察页面', state: 'waiting', run: async () => {
        const context = ['/workbench', '/auction'].includes(initial.pathname)
          ? [...q].filter(([key]) => ['trade_day', 'as_of_time'].includes(key)) : [];
        const current = terminalView(initial.pathname, initial.search);
        const pending = [...terminalTabs].sort(([a], [b]) => Number(b === current) - Number(a === current)).map(([view]) => {
          const params = new URLSearchParams({ view });
          for (const [key, value] of context) params.set(key === 'trade_day' ? 'day' : key === 'instrument' ? 'instrument_id' : key, value);
          if (params.has('stock')) params.delete('instrument_id');
          return params;
        });
        // Queue all four durable projections before waiting for one cold view.
        // A warming response is expected and must not abandon the other views.
        while (pending.length && !this.stopped) {
          for (const params of [...pending]) {
            try {
              const result = await read(() => loadWorkbench(params));
              prepareTerminalDetails(result.data, params.get('as_of_time') ?? ''); startPreparedDetails();
              pending.splice(pending.indexOf(params), 1);
            } catch (error) {
              if (!(error instanceof ApiError) || error.code !== 'read_cache_warming') throw error;
            }
          }
          if (pending.length) await new Promise(resolve => window.setTimeout(resolve, 1_000));
        }
      } },
      { label: '资讯中心 · 资讯与日报周报月报', state: 'waiting', run: async () => {
        const category = initial.pathname === '/news' ? newsCategory(q, preferences) : preferences.news_category;
        const newsParams = new URLSearchParams({ category, limit: '30', order: initial.pathname === '/news' && q.get('order') === 'earliest' ? 'earliest' : 'latest' });
        if (initial.pathname === '/news') for (const key of ['q', 'family', 'day', 'saved', 'unread', 'following']) if (q.has(key)) newsParams.set(key, q.get(key)!);
        await read(() => newsApi.items(newsParams));
        await read(() => newsApi.items(new URLSearchParams({ category: '全部', limit: '1' })));
        for (const period of ['day', 'week', 'month'] as const) {
          const archive = await read(() => newsApi.editions(period));
          if (archive.data.items[0]) await read(() => newsApi.edition(archive.data.items[0].id));
        }
      } },
      { label: '数据管理 · 接入与运行状态', state: 'waiting', run: async () => {
        if (admin) { await read(dataCenterApi.integrations); await read(dataCenterApi.settings); }
        await read(dataCenterApi.overview);
        await read(() => dataCenterApi.calls({}));
      } },
    ];
    // The mounted page starts its reads first. The background queue then warms
    // that module first, with at most two independent preparation groups.
    const first = reviewHere ? 0 : initial.pathname === '/news' ? 3 : initial.pathname === '/data' ? 4 : 2;
    this.tasks = [this.tasks[first], ...this.tasks.filter((_, index) => index !== first)];
    if (initial.pathname === '/system') this.tasks = [];
    this.emit();
  }

  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private emit() { this.snapshot = this.tasks.map(({ label, state, error }) => ({ label, state, error })); this.listeners.forEach(listener => listener()); }
  start = () => { this.stopped = false; if (!this.started) { this.started = true; window.setTimeout(() => { if (!this.stopped) void this.run(); }, 300); } };
  retry = () => {
    if (this.running || this.stopped) return;
    this.tasks.forEach(task => { if (task.state === 'failed') { task.state = 'waiting'; delete task.error; } });
    this.emit(); void this.run();
  };
  private cancel = () => { this.stopped = true; };
  private clear = () => { this.stopped = true; this.tasks = []; this.emit(); };
  dispose = () => { this.stopped = true; };
  private async run() {
    this.running = true;
    window.addEventListener('session-changed', this.cancel);
    window.addEventListener('session-expired', this.cancel);
    window.addEventListener('read-cache-cleared', this.clear);
    const worker = async () => {
      while (!this.stopped) {
        const task = this.tasks.find(item => item.state === 'waiting');
        if (!task) break;
        task.state = 'loading'; this.emit();
        try { await task.run(); task.state = 'ready'; }
        catch (error) { task.state = 'failed'; task.error = error instanceof Error ? error.message : '暂时无法读取，可稍后重试。'; }
        if (!this.stopped) this.emit();
      }
    };
    await Promise.all([worker(), worker()]);
    if (!this.stopped) startPreparedDetails();
    this.running = false;
    window.removeEventListener('session-changed', this.cancel);
    window.removeEventListener('session-expired', this.cancel);
    window.removeEventListener('read-cache-cleared', this.clear);
  }
}
