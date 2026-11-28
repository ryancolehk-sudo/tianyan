import type { ApiReviewInput, PlanApiConnection } from './model';
import { ReviewMaterials, type ReviewControls } from './ReviewMaterials';

const statusText: Record<PlanApiConnection['state'], string> = {
  ready: '已启用', needs_enable: '测试已通过 · 待启用', needs_test: '当前配置待测试',
  test_failed: '上次连接测试失败', test_unknown: '上次测试结果待核对', needs_model: '尚未填写模型 ID', needs_key: 'API Key 缺失或不可用',
};

type Props = ReviewControls & { connections: PlanApiConnection[]; selectedId?: string;
  select: (id: string) => void; manage: (id: string) => void; generate: (input: ApiReviewInput) => void };

export function ApiReview({ connections, selectedId, select, manage, generate, ...controls }: Props) {
  const connection = connections.find(item => item.id === selectedId) ?? connections.find(item => item.state === 'ready')
    ?? connections.find(item => item.state === 'needs_enable') ?? connections[0];
  const { id, model, state } = connection;
  const needsEnable = state === 'needs_enable';
  const usable = state === 'ready' || needsEnable;
  const action = needsEnable ? '启用并复盘' : '开始复盘';
  return <section className="pr-direct-codex pr-direct-api" aria-label="用我的 API 复盘">
    <div className="pr-api-connection">
      {connections.length > 1 ? <label className="pr-model-line">API 模型<select value={id} disabled={controls.busy}
        onChange={e => select(e.target.value)}>{connections.map(item =>
          <option value={item.id} key={item.id}>{item.name} · {item.model || '待选模型'}</option>)}</select></label> :
        <strong>{connection.name}{model && model !== connection.name && <span> · {model}</span>}</strong>}
      <span className="pr-api-state">{statusText[state]}</span>
      {usable && controls.admin && <button className="pr-custom-link" disabled={controls.busy} onClick={() => manage(id)}>管理 API</button>}
    </div>
    {!usable ? <div className="pr-choice-next">{controls.admin
      ? <button className="pr-generate" onClick={() => manage(id)}>{state === 'needs_test' || state === 'test_failed' ? '测试此 API' : '查看 API 配置'}</button>
      : <span>请联系管理员完成 API 配置和测试。</span>}</div> :
      <ReviewMaterials key={id} {...controls}>{(scope, disabled, start) => <div className="pr-choice-next">
        {needsEnable && !controls.admin ? <span>请联系管理员启用这个 API。</span> :
          <button type="button" className="pr-generate" disabled={disabled} aria-label={action + ' · ' + model}
            onClick={() => start(() => generate({...scope, connection_id: id, connection_revision: connection.revision,
              ...(needsEnable && {enable_connection: true})}))}>{controls.busy ? '正在提交…' : action}</button>}
      </div>}</ReviewMaterials>}
  </section>;
}
