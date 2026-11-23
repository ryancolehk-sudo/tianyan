import type { SessionResponse } from '../../generated/schema';
import type { Day, Ref, Series, StockDetail, Theme, ThemeHistory } from './model';
import type { Notebook } from './notes';
export type ReviewProps = {
  session: SessionResponse; day: string; asOf: string; data: Day; series: Series | null; seriesBusy: boolean;
  notebook: Notebook; sector: string; stock: string; selectedTheme: Theme | null; detailBusy: boolean;
  tracked: StockDetail[]; watched: Set<string>; mine: boolean;
  themeHistory: ThemeHistory | null; themeHistoryBusy: boolean; themeHistoryError: Error | null; reloadThemeHistory: () => void;
  stagesByDay: Record<string, string>; themeStagesByDay: Record<string, Array<{ sector_code: string; stage: string }>>;
  select: (values: Record<string, string | null>, replace?: boolean) => void;
  openRef: (ref: Ref) => void; edit: (section: string) => void; go: (view: 'emotion' | 'direction' | 'lhb' | 'overview') => void;
  addCore: (code: string, name: string, sector?: string | null) => void;
};
