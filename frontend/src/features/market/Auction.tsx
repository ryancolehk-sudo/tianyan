import { useCallback, useState } from 'react';
import { Icon } from '../../shared/icons';
import type { SessionResponse } from '../../generated/schema';
import { AuctionNotebook } from './AuctionNotebook';
import { AuctionMarket } from './AuctionMarket';
import { AuctionVendor } from './AuctionVendor';
import { today } from './auctionFormat';

export function Auction({ session }: { session: SessionResponse }) {
  const [explicitDay, setExplicitDay] = useState(() => /^\d{4}-\d{2}-\d{2}$/.test(new URLSearchParams(location.search).get('date') ?? ''));
  const [day, setDay] = useState(() => explicitDay ? new URLSearchParams(location.search).get('date')! : today());
  const changeDay = useCallback((value: string) => { setExplicitDay(true); setDay(value); }, []);
  const latestDay = useCallback((value: string) => { if (!explicitDay) setDay(value); }, [explicitDay]);
  return <><AuctionVendor day={day} session={session} onLatestDay={latestDay} onDayChange={changeDay}/>
    <div id="auction-notebook"><AuctionNotebook session={session}/></div>
    <details className="auction-other-sources"><summary>其他来源已存竞价快照</summary><AuctionMarket session={session} day={day} onDayChange={changeDay}/></details>
    <div className="research-status-note"><Icon name="info"/><span>人工观察可保存并回查，竞价监控与交易执行未启用。没有倒计时、假快照或默认风险结论。</span></div></>;
}
