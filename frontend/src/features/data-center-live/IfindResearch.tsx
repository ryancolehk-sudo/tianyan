import { useRef, useState } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { request } from '../../shared/api';
import type { ConnectionView, Envelope } from './api';
import { ErrorNotice, Panel, Status, useResource } from './shared';

type Domain = { id: string; name: string; tools: string[] };
type Result = { connection_id: string; revision: number; domain: string; status: string; received_at: string | null; text: string | null; tools: { name: string; inputSchema: unknown }[]; error_code: string | null; limitations: string[] };
const toolNames: Record<string, string> = {
  index_data: '指数数据', sector_data: '板块与成分股', index_highfreq_quotes: '指数实时与高频行情',
  get_stock_summary: '股票概况', search_stocks: '选股查询', get_stock_performance: '股票表现', get_stock_info: '公司资料',
  get_stock_shareholders: '股东信息', get_stock_financials: '财务数据', get_risk_indicators: '风险指标', get_stock_events: '公司事件', get_esg_data: 'ESG数据', stock_highfreq_quotes: '股票实时与高频行情',
  search_funds: '基金筛选', get_fund_profile: '基金资料', get_fund_market_performance: '基金表现', get_fund_ownership: '持有人结构', get_fund_portfolio: '基金持仓', get_fund_financials: '基金财务', get_fund_company_info: '基金公司', fund_highfreq_quotes: '基金实时与高频行情',
  get_edb_data: '宏观与行业指标', search_edb: '搜索经济指标', search_notice: '公告搜索', search_news: '新闻搜索', search_trending_news: '热点新闻',
  bond_basic_info: '债券资料', bond_market_data: '债券行情', bond_financial_data: '债券财务', bond_special_data: '债券专题', bond_highfreq_quotes: '债券实时与高频行情',
  search_global_stocks: '港美股筛选', global_stock_profile: '港美股资料', global_stock_quotes: '港美股行情', global_stock_financial: '港美股财务', global_stock_events: '港美股事件', future_profile: '期货合约资料', future_quotes: '期货行情',
};
function answerText(text: string) {
  try { const value = JSON.parse(text); return typeof value?.data?.answer === 'string' ? value.data.answer : text; } catch { return text; }
}
const loadDomains = () => request<Envelope<Domain[]>>('/api/v1/integrations/ifind/domains');

export function IfindResearch({ connections, session, configure }: { connections: ConnectionView[]; session: SessionResponse; configure: (id: string) => void }) {
  const domains = useResource(loadDomains);
  const [connectionId, setConnectionId] = useState('');
  const [domain, setDomain] = useState('index');
  const [tool, setTool] = useState('index_data');
  const [query, setQuery] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const [argumentsText, setArgumentsText] = useState('{}');
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  // Retain the same idempotency key after network uncertainty. No automatic retries.
  const pending = useRef<{ signature: string; key: string } | null>(null);
  const items = connections.filter(item => item.template === 'ifind_mcp');
  const connection = items.find(item => item.id === connectionId) ?? items[0];
  const ready = !!connection?.enabled && connection.tested_revision === connection.revision;
  const admin = session.data.user.role === 'admin';
  async function run(catalog: boolean) {
    if (!connection || lock.current) return;
    lock.current = true; setBusy(true); setError(null); setResult(null);
    try {
      const args = catalog ? {} : advanced ? JSON.parse(argumentsText) : { query };
      const body = { domain, tool: catalog ? null : tool, arguments: args, expected_revision: connection.revision };
      const signature = JSON.stringify([connection.id, body]);
      if (pending.current?.signature !== signature) pending.current = { signature, key: crypto.randomUUID() };
      const value = await request<Envelope<Result>>(`/api/v1/integrations/${encodeURIComponent(connection.id)}/ifind/query`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session.data.csrf_token, 'Idempotency-Key': pending.current.key }, body: JSON.stringify(body),
      });
      setResult(value.data);
      if (!['running', 'unknown'].includes(value.data.status)) pending.current = null;
    } catch (reason) { setError(reason); } finally { lock.current = false; setBusy(false); }
  }
  return <Panel title="iFinD 官方金融研究" className="dc-ifind">
    <p>八个细分领域 · 手动查询 · 与原行情采集独立。仅使用当前账户权益，不自动购买或补充额度。</p>
    {!connection ? <p>在下方“其他来源与高级采集配置”添加“同花顺 iFinD 官方 MCP”，保存密钥并检测、启用。</p> : <>
      <fieldset disabled={busy || !admin} className="dc-form-fields">
        <label>接入服务<select value={connection.id} onChange={e => { setConnectionId(e.target.value); setResult(null); }}>{items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label>金融领域<select value={domain} onChange={e => { setDomain(e.target.value); setTool(domains.data?.data.find(item => item.id === e.target.value)?.tools[0] ?? ''); setResult(null); }}>{domains.data?.data.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label>查询能力<select value={tool} onChange={e => { setTool(e.target.value); setResult(null); }}>{domains.data?.data.find(item => item.id === domain)?.tools.map(name => <option key={name} value={name}>{toolNames[name] ?? name}</option>)}</select></label>
        <label className="dc-ifind-query">查询内容<input value={query} onChange={e => { setQuery(e.target.value); setResult(null); }} placeholder="写明标的、日期和指标" maxLength={2000}/></label>
        <label className="dc-ifind-advanced"><input type="checkbox" checked={advanced} onChange={e => { setAdvanced(e.target.checked); setResult(null); }}/>使用结构化参数（高频行情等）</label>
        {advanced && <label className="dc-ifind-query">参数 JSON<textarea value={argumentsText} onChange={e => { setArgumentsText(e.target.value); setResult(null); }} maxLength={8000} rows={4}/></label>}
      </fieldset>
      <div className="dc-form-actions"><button disabled={busy || !admin || !ready} onClick={() => void run(true)}>读取本领域工具说明</button><button className="dc-primary" disabled={busy || !admin || !ready || (!advanced && !query.trim())} onClick={() => void run(false)}>{busy ? '查询中…' : '查询一次'}</button><button disabled={busy} onClick={() => configure(connection.id)}>管理接入</button></div>
      {!ready && <p>连接尚未启用或配置需要重新检测。</p>}
      <p className="dc-muted">本机每日最多8次取数；试用余额请在iFinD账户查看。目录可读不代表取数或全市场分钟覆盖通过。</p>
    </>}
    {(error || domains.error) && <ErrorNotice error={error || domains.error}/>}
    {result && result.connection_id === connection?.id && result.revision === connection?.revision && result.domain === domain && <div aria-live="polite"><Status value={result.status}/>{result.received_at && <p>接收时间：{new Date(result.received_at).toLocaleString()} · 不是行情时刻</p>}{result.error_code && <p>本次未通过：{result.error_code}。未自动重试。</p>}{result.text && <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 360, overflow: 'auto' }}>{answerText(result.text)}</pre>}{!!result.tools.length && <details><summary>查看工具参数说明</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 260, overflow: 'auto' }}>{JSON.stringify(result.tools, null, 2)}</pre></details>}{result.limitations.map(text => <p className="dc-muted" key={text}>{text}</p>)}</div>}
  </Panel>;
}
