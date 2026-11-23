export function dateContext(requested: string, received: string, marketDay: string, asOf: string) {
  if (!received) return {state:'loading',message:'正在核对资料日期'};
  if (requested && requested !== received) return {state:'mismatch',message:`所选日期 ${requested} 与返回资料 ${received} 不一致；以下保留原资料日期，请重读核对。`};
  if (asOf) return {state:'history',message:`历史时点回看 · 资料日 ${received}`};
  if (requested && marketDay && requested !== marketDay) return {state:'history',message:`历史日期 ${requested} · 当前交易日 ${marketDay}`};
  if (!requested && marketDay && received !== marketDay) return {state:'lagging',message:`当前交易日 ${marketDay} · 最近返回资料 ${received}，资料尚未同步至当前交易日。`};
  return {state:'current',message:`资料日期 ${received}`};
}
