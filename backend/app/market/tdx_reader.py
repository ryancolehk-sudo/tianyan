"""Isolated native SDK worker; stdout contains only a controlled JSON envelope."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys


def run(request):
    from app.market.tdx_data import MAX_SELECTION, SNAPSHOT_FIELDS
    from app.market.connected_specs import TDX_FAMILIES
    key=request.get('api_key');codes=request.get('codes');family=request.get('family')
    count_file=Path(request['private_lib_dir'])/'request-count'
    def count(value):count_file.write_text(str(value),encoding='ascii')
    count(0)
    if not isinstance(key,str) or not re.fullmatch(r'TDX-[A-Za-z0-9_-]{8,240}',key):return {'error':'tdx_data_permission_missing'}
    if family not in TDX_FAMILIES or not isinstance(codes,list) or not 1<=len(codes)<=MAX_SELECTION or any(
        not isinstance(c,str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)',c) for c in codes):return {'error':'schema_changed'}
    spec=importlib.util.find_spec('tdxaidata')
    if spec is None or not spec.origin:return {'error':'tdx_dependency_missing'}
    native_dir=Path(spec.origin).parent/'lib'
    library={'win32':'TdxAiData.dll','darwin':'libTdxAiData.dylib'}.get(sys.platform,'libTdxAiData.so')
    if not (native_dir/library).is_file():return {'error':'tdx_dependency_missing'}
    # Cleanup belongs to the parent AFTER this process exits: Windows keeps a
    # loaded DLL locked, including while returning from this function.
    root=Path(request['private_lib_dir'])
    for item in native_dir.iterdir():
        if item.is_file():shutil.copy2(item,root/item.name)
    for filename in ('TdxAiData.ini','user.ini'):
        ini=root/filename
        original=ini.read_text(encoding='utf8',errors='replace') if ini.exists() else ''
        original=re.sub(r'(?ims)^\[Token\].*?(?=^\[|\Z)','',original)
        ini.write_text(original.rstrip()+'\n[Token]\ntoken='+key+'\n',encoding='utf8')
    os.environ['TDX_AI_DATA_LIB']=str(root/library)
    from tdxaidata import tqs
    if family in {'tdx_quotes','tdx_depth5'}:
        count(1)
        rows=tqs.get_market_snapshot_batch(stock_list=codes,field_list=list(SNAPSHOT_FIELDS))
        if not isinstance(rows,dict) or not rows:return {'error':'tdx_no_data','request_count':1}
        return {'rows':rows,'request_count':1}
    from tdxaidata.tdxaidata import _NativeTdxAiData
    from datetime import date
    day=date.fromisoformat(request['day'])
    rows={};failed=[];exception_count=0
    for index,code in enumerate(codes):
        count(index+1)
        try:
            raw=_NativeTdxAiData.get_instance().get_his_dats(code,'',day.isoformat()+' 23:59:59','1m','none',480)
            value=tqs._clean(raw)
            if isinstance(value,dict):value=value.get('Value',value)
            if isinstance(value,dict) and value.get('Date'):rows[code]=value
            else:failed.append(code)
        except Exception:failed.append(code);exception_count+=1
    if not rows:return {'error':'tdx_sdk_unavailable' if exception_count==len(codes) else 'tdx_no_data','request_count':len(codes)}
    return {'rows':rows,'failed_codes':failed,'request_count':len(codes)}


if __name__=='__main__':
    try:
        request=json.loads(sys.stdin.read(32000))
        # Native libraries can write diagnostic content directly to descriptors.
        # Redirect both for the full lifetime, then emit only our envelope.
        saved=os.dup(1)
        with open(os.devnull,'w') as quiet:
            os.dup2(quiet.fileno(),1);os.dup2(quiet.fileno(),2)
            with contextlib.redirect_stdout(quiet),contextlib.redirect_stderr(quiet):
                try:result=run(request)
                except Exception:result={'error':'tdx_sdk_unavailable'}
        os.write(saved,(json.dumps(result,ensure_ascii=True)+'\n').encode('utf8'))
        os.close(saved)
    except Exception:
        pass
