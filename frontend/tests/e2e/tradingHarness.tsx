import { NotificationButton } from '../../src/shared/BackgroundRefresh';
// Browser-only synthetic test entry. Not imported by the application or build.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { FundsReference, PoolsReference, upStatText } from '../../src/features/market/MairuiReference';
import { NewsPreview } from '../../src/features/market/News';
import { SectorMarkets } from '../../src/features/market/SectorHeat';
import { ResearchSummary } from '../../src/features/workbench/ResearchSummary';
import { ReferenceWidget } from '../../src/features/workbench/ReferenceWidgets';
import { LeaderboardWidget, TradingDatasetView, amountText, groupLeaderboard, projectTradingDay } from '../../src/features/workbench/TradingWidgets';
import '../../src/tokens.css';

const harness = window as unknown as { tradingFixture:{view:string;props:Record<string,unknown>};tradingTest:unknown };
harness.tradingTest = {upStatText,amountText,groupLeaderboard,projectTradingDay};
const components = {funds:FundsReference,pools:PoolsReference,leaderboard:LeaderboardWidget,news:NewsPreview,sectors:SectorMarkets,research:ResearchSummary,dataset:TradingDatasetView,reference:ReferenceWidget};
const Component = components[harness.tradingFixture.view as keyof typeof components];
createRoot(document.getElementById('test-root')!).render(React.createElement(React.Fragment,null,React.createElement(NotificationButton),React.createElement(Component as React.ComponentType<Record<string,unknown>>,harness.tradingFixture.props)));
