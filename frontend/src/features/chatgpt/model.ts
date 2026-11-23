export type ChatGPTEnvelope<T> = { data: T; meta?: Record<string, unknown> };

export type ChatGPTAccount = {
  id: string; connection_id: string; email: string | null; label: string;
  state: 'connected' | 'identity_only' | 'disconnected' | 'refreshing' | 'refresh_unknown' | 'reauthorization_required' | 'client_error';
  use_for_ai: boolean; sharing_granted: boolean; model: string | null; enabled: boolean;
  revision: number; tested: boolean;
  last_test: { status?: string; at?: string | null; error_code?: string | null; elapsed_ms?: number | null; revision?: number | null } | null;
  welcome_pending: boolean; created_at: string; updated_at: string;
  reauthorization_required: boolean; remote_revocation_unconfirmed: boolean;
};

export type ChatGPTStatus = {
  available: boolean; local_only: boolean; reason: string | null; authenticated: boolean; accounts: ChatGPTAccount[];
};

export const chatGPTUsageURL = 'https://chatgpt.com/settings/usage';

// The OAuth response stays in this stack frame, never in React state, URL state
// or browser storage. The server owns the callback and token exchange.
export function continueToChatGPT(authorizationURL: string) {
  let target: URL;
  try { target = new URL(authorizationURL); }
  catch { throw new Error('登录地址无效，请重新发起官方登录。'); }
  if (target.protocol !== 'https:' || target.hostname !== 'auth.openai.com' || target.port || target.username || target.password) {
    throw new Error('登录地址未通过官方地址校验，请重新发起登录。');
  }
  window.location.assign(target.href);
}

const callbackMessages: Record<string, string> = {
  connected: 'ChatGPT 账号已连接。登录授权与模型生成测试分别记录，请继续选择模型并手动测试。',
  signed_in: '已使用 ChatGPT 身份登录独立工作空间。请检查计划授权与模型状态；原账号资料不会自动合并。',
  chatgpt_consent_denied: '你取消了 ChatGPT 授权，可以重新登录。原工作空间不受影响。',
  chatgpt_access_denied: '你取消了 ChatGPT 授权，可以重新登录。',
  chatgpt_state_invalid: '本次登录已失效或校验未通过，请从本机页面重新开始。',
  chatgpt_login_expired: '本次登录已超时，请重新开始。',
  chatgpt_account_mismatch: '本次授权的 ChatGPT 账号与所选账号不一致，请选择正确账号重新登录。',
  chatgpt_identity_conflict: '这个 ChatGPT 身份已关联其他工作空间，请先登录原工作空间。',
  chatgpt_plan_not_granted: '已验证登录身份，但尚未授予计划使用权限。请重新授权后检查。',
  chatgpt_local_only: '当前接入只支持本机访问，请在运行天衍的电脑打开本机入口。',
};

export function chatGPTCallbackMessage(value: string | null) {
  if (!value) return null;
  return callbackMessages[value] ?? 'ChatGPT 登录未完成，请重新发起；现有工作空间和资料保持不变。';
}
