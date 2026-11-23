let running=false;
const requestedJob=new URLSearchParams(location.search).get('job');
async function refresh(){
  if(running)return;
  running=true;
  try{
    const response=await fetch('/maintenance/status.json',{cache:'no-store'});
    if(response.status===404){document.querySelector('h1').textContent='暂无维护任务';document.querySelector('#connection').textContent='尚无本机升级器状态记录。可返回系统设置查看连接状态。';return;}
    if(!response.ok)throw new Error('unavailable');
    const data=await response.json();
    if(requestedJob&&data.job_id!==requestedJob){document.querySelector('h1').textContent='等待本次升级开始';document.querySelector('#connection').textContent='本机升级器尚未发布本次任务的状态；上一次维护结果不代表本次升级。';return;}
    document.querySelector('h1').textContent= data.stage==='succeeded'?'升级完成':data.stage==='manual_recovery'?'需要按指引恢复':data.terminal?'本次维护已结束':'维护正在进行';
    for(const [id,value] of Object.entries({message:data.message,stage:data.stage,time:new Date(data.updated_at).toLocaleString('zh-CN'),backup:data.backup_available?'完整备份已保留；恢复验证请查看备份记录':'尚未生成升级前备份',error:data.error_code?'问题编号：'+data.error_code:''}))document.getElementById(id).textContent=String(value||'—');
    document.querySelector('#connection').textContent=Date.now()-Date.parse(data.updated_at)>120000&&!data.terminal?'近期没有收到新状态，请保留此页面并检查本机升级器。':'此状态不依赖正在重启的网页服务接口。';
  }catch{document.querySelector('#connection').textContent='暂时无法读取维护状态，已保留上一条进度。稍后会再次核对。';}
  finally{running=false;}
}
document.querySelector('#retry').addEventListener('click',refresh);
refresh();setInterval(refresh,3000);
