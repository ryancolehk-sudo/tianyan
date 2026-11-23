import { lazy, Suspense } from 'react';
import type { SessionResponse } from '../generated/schema';
import { Card, Empty } from '../shared/ui';
import { researchRoutes } from './navigation';
import { terminalView } from '../shared/terminalNavigation';
import { useNavigation } from '../shared/navigation';
import { isDailyReview } from '../shared/reviewNavigation';
import { PageBoundary } from '../shared/PageBoundary';
import { Terminal } from './terminal/Terminal';
const Research = lazy(() => import('./research/Research').then(module => ({ default: module.Research })));
const Validations = lazy(() => import('./research/Validations').then(module => ({ default: module.Validations })));
const News = lazy(() => import('./market/News').then(module => ({ default: module.News })));
const DataCenter = lazy(() => import('./data/DataCenter').then(module => ({ default: module.DataCenter })));
const SystemSettings = lazy(() => import('./system-settings/SystemSettings').then(module => ({ default: module.SystemSettings })));
const Workbench = lazy(() => import('./workbench/Workbench').then(module => ({ default: module.Workbench })));
const Auction = lazy(() => import('./market/Auction').then(module => ({ default: module.Auction })));
const DailyReview = lazy(() => import('./daily-review/DailyReview').then(module => ({ default: module.DailyReview })));
const Tomorrow = lazy(() => import('./tomorrow/Tomorrow').then(module => ({ default: module.Tomorrow })));

export function Page({ path, session }: { path: string; session: SessionResponse }) {
  const navigation = useNavigation();
  return <PageBoundary context={path + navigation.search + navigation.hash}><PageContent path={path} session={session}/></PageBoundary>;
}

function PageContent({ path, session }: { path: string; session: SessionResponse }) {
  const navigation = useNavigation();
  const legacy = new URLSearchParams(navigation.search).get('legacy') === '1';
  if (path === '/research/tomorrow') return <Suspense fallback={<p role="status">正在载入明日预案…</p>}><Tomorrow session={session}/></Suspense>;
  if (isDailyReview(path, navigation.search, navigation.hash)) return <Suspense fallback={<p role="status">正在载入每日复盘…</p>}><DailyReview session={session}/></Suspense>;
  if ((path === '/workbench' || path === '/auction') && !legacy) return <Terminal session={session} view={terminalView(path, navigation.search)}/>;
  if (path === '/workbench') return <Suspense fallback={<p role="status">正在载入历史工作台…</p>}><Workbench session={session}/></Suspense>;
  if (path === '/auction') return <Suspense fallback={<p role="status">正在载入原竞价观察…</p>}><Auction session={session}/></Suspense>;
  if (path === '/news') return <Suspense fallback={<p role="status">正在载入资讯中心…</p>}><News/></Suspense>;
  if (path === '/research/validations') return <Suspense fallback={<p role="status">正在载入验证记录…</p>}><Validations session={session}/></Suspense>;
  if (researchRoutes.some(([route]) => route === path)) return <Suspense fallback={<p role="status">正在载入研究记录…</p>}><Research path={path} session={session}/></Suspense>;
  if (path === '/data') return <Suspense fallback={<p role="status">正在载入数据管理…</p>}><DataCenter session={session}/></Suspense>;
  if (path === '/system') return <Suspense fallback={<p role="status">正在载入系统设置…</p>}><SystemSettings session={session}/></Suspense>;
  return <Card title="页面不存在"><Empty title="没有找到这个页面"><a className="text-link" href="/workbench">返回工作台</a></Empty></Card>;
}
