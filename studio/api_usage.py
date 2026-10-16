"""Privacy-preserving DeepSeek request accounting for the local Studio.

Only provider-returned usage is billed. Prompt metrics are structural byte
counts for diagnostics; they are never presented as token estimates.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import codecs
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import uuid
from zoneinfo import ZoneInfo

from . import core

_context: ContextVar[dict] = ContextVar("studio_api_usage_context", default={})
_write_lock = threading.Lock()
_rates = {
    "deepseek-flash": {"hit": Decimal("0.02"), "miss": Decimal("1"), "output": Decimal("4")},
    "deepseek-v4.1-flash": {"hit": Decimal("0.02"), "miss": Decimal("1"), "output": Decimal("4")},
    "deepseek-v4-flash": {"hit": Decimal("0.02"), "miss": Decimal("1"), "output": Decimal("4")},
    "deepseek-v4-flash-vision-exp": {"hit": Decimal("0.02"), "miss": Decimal("1"), "output": Decimal("4")},
    "deepseek-v4-pro": {"hit": Decimal("0.15"), "miss": Decimal("4.5"), "output": Decimal("13.5")},
    "deepseek-v4-pro-0813": {"hit": Decimal("0.15"), "miss": Decimal("4.5"), "output": Decimal("13.5")},
}
_warn_token_threshold = 250_000


def set_context(*, request_id=None, task_id=None, project_id=None, slide_id=None, module=None, issue_id=None):
    value={**_context.get()}
    for key,item in locals().copy().items():
        if key!="value" and item is not None:value[key]=str(item)
    return _context.set(value)


def reset_context(token):
    _context.reset(token)


def context():
    return dict(_context.get())


def _usage_dir() -> Path:
    return Path(os.environ.get("PPT_STUDIO_USAGE_DIR", str(core.DATA / "api_usage"))).expanduser()


def _jsonl_path(name="requests.jsonl") -> Path:
    path=_usage_dir()/name
    path.parent.mkdir(parents=True,exist_ok=True)
    return path


def _append(name: str, row: dict) -> None:
    try:
        path=_jsonl_path(name)
        line=json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n"
        with _write_lock, path.open("a",encoding="utf-8") as stream:
            try:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            except (ImportError,OSError):
                pass
            stream.write(line);stream.flush()
            try:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
            except (ImportError,OSError):
                pass
    except Exception as exc:
        # Monitoring must never turn a completed/valid model action into a failure.
        logging.getLogger(__name__).warning("Local API usage telemetry write failed (%s)",type(exc).__name__)


def clear_records(*, project_id=None) -> dict:
    """Clear all local usage records, or just one project's usage ledger."""
    cleared = {}
    with _write_lock:
        for name in ("requests.jsonl", "outcomes.jsonl", "transfers.jsonl"):
            path = _usage_dir() / name
            try:
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                removed = 0
                retained = []
                for line in lines:
                    if not line.strip():
                        retained.append(line)
                        continue
                    if project_id is None:
                        removed += 1
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        retained.append(line)
                        continue
                    if isinstance(row, dict) and row.get("project_id") == project_id:
                        removed += 1
                    else:
                        retained.append(line)
                if removed:
                    path.write_text("".join(retained), encoding="utf-8")
                count = removed
                cleared[name] = count
            except FileNotFoundError:
                cleared[name] = 0
    return {"cleared": cleared, "total": sum(cleared.values())}


def _read(name="requests.jsonl"):
    path=_usage_dir()/name
    if not path.is_file():return []
    rows=[]
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row=json.loads(line)
                    if isinstance(row,dict):rows.append(row)
                except (ValueError,TypeError):continue
    except OSError: return []
    return rows


def _read_recent(limit=2000, max_bytes=4_000_000):
    """Read a bounded tail for in-task anomaly comparisons."""
    path=_usage_dir()/"requests.jsonl"
    if not path.is_file():return []
    try:
        with path.open("rb") as stream:
            stream.seek(0,os.SEEK_END);size=stream.tell();stream.seek(max(0,size-max_bytes))
            raw=stream.read().decode("utf-8",errors="replace")
        lines=raw.splitlines()
        if size>max_bytes and lines:lines=lines[1:]
        rows=[]
        for line in lines[-limit:]:
            try:
                row=json.loads(line)
                if isinstance(row,dict):rows.append(row)
            except (ValueError,TypeError):continue
        return rows
    except OSError:return []


def message_metrics(messages) -> dict:
    """Measure structure/bytes without retaining message contents."""
    counts=Counter();sizes=Counter();image_count=0
    def walk(value, role="unknown", parent=""):
        nonlocal image_count
        if isinstance(value,dict):
            kind=str(value.get("type", "" )).lower()
            if kind == 'file' and isinstance(value.get('file_id'), str):
                image_count+=1;counts['images']+=1;counts['image_references']+=1
                sizes['image_reference_bytes']+=len(value['file_id'].encode())
            elif kind in {"image_url","input_image"} or "image_url" in value or "image" in value and isinstance(value.get("image"),str):
                image_count+=1;counts["images"]+=1
                raw=value.get("image_url",{}).get("url","") if isinstance(value.get("image_url"),dict) else value.get("image_url","")
                if isinstance(raw,str):sizes["image_payload_bytes"]+=len(raw.encode("utf-8",errors="replace"))
            for key,item in value.items():
                low=str(key).lower()
                if low in {"content","text","arguments"} and isinstance(item,str):
                    category="tool_result_text" if role=="tool" else ("tool_call_arguments" if parent=="tool_call" else f"{role}_text")
                    counts[category]+=1;sizes[category+"_bytes"]+=len(item.encode("utf-8",errors="replace"))
                elif isinstance(item,(dict,list)):
                    walk(item,role,"tool_call" if low in {"tool_calls","function"} else low)
                # Deliberately ignore scalar metadata and never serialize it.
        elif isinstance(value,list):
            for item in value:walk(item,role,parent)
    if isinstance(messages,list):
        for message in messages:
            role=str(message.get("role","unknown")) if isinstance(message,dict) else "unknown"
            counts["messages"]+=1;walk(message,role)
    return {"message_count":counts["messages"],"image_count":image_count,
            "component_counts":dict(counts),"component_bytes":dict(sizes),
            "content_bytes_total":sum(sizes.values())}


def normalize_usage(payload) -> dict:
    """Normalize actual Chat Completions or Responses API usage fields."""
    if not isinstance(payload,dict):return {"known":False}
    # A full API response, a usage object, or an SSE final chunk is accepted.
    usage=payload.get("usage") if isinstance(payload.get("usage"),dict) else payload
    if not isinstance(usage,dict):return {"known":False}
    input_tokens=usage.get("prompt_tokens",usage.get("input_tokens"))
    output_tokens=usage.get("completion_tokens",usage.get("output_tokens"))
    total_tokens=usage.get("total_tokens")
    hit=usage.get("prompt_cache_hit_tokens")
    miss=usage.get("prompt_cache_miss_tokens")
    prompt_details=usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if hit is None:hit=prompt_details.get("cached_tokens") if isinstance(prompt_details,dict) else None
    if miss is None and isinstance(input_tokens,int) and isinstance(hit,int):miss=max(0,input_tokens-hit)
    out_details=usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    reasoning=out_details.get("reasoning_tokens") if isinstance(out_details,dict) else None
    fields={"input_tokens":input_tokens,"cache_hit_tokens":hit,"cache_miss_tokens":miss,
            "output_tokens":output_tokens,"reasoning_tokens":reasoning,"total_tokens":total_tokens}
    for key,value in fields.items():
        if isinstance(value,bool) or not isinstance(value,int) or value<0:fields[key]=None
    fields["known"]=fields["input_tokens"] is not None or fields["output_tokens"] is not None or fields["total_tokens"] is not None
    return fields


def _holiday_set():
    return {x.strip() for x in os.environ.get("PPT_STUDIO_CN_HOLIDAYS","").split(",") if re.fullmatch(r"\d{4}-\d{2}-\d{2}",x.strip())}


def price_window(timestamp: str) -> tuple[bool,str,bool]:
    dt=datetime.fromisoformat(timestamp.replace("Z","+00:00")).astimezone(ZoneInfo("Asia/Shanghai"))
    holiday=dt.date().isoformat() in _holiday_set()
    peak=dt.weekday()<5 and (9<=dt.hour<12 or 14<=dt.hour<18) and not holiday
    if holiday:return False,"off_peak_chinese_holiday",True
    if not (dt.weekday()<5 and (9<=dt.hour<12 or 14<=dt.hour<18)):
        return False,"off_peak",True
    complete_years={x.strip() for x in os.environ.get("PPT_STUDIO_CN_HOLIDAYS_COMPLETE_YEARS","").split(",") if x.strip().isdigit()}
    return peak,"peak" if dt.year in {int(x) for x in complete_years} else "peak_calendar_unverified",dt.year in {int(x) for x in complete_years}


def calculate_cost(model: str, timestamp: str, usage: dict, provider: str | None = None) -> dict:
    normalized=normalize_usage(usage)
    provider=(provider or os.environ.get('PPT_STUDIO_API_PROVIDER','deepseek')).strip().lower()
    normalized_model=str(model or '').lower()
    if provider=='qwen' and (normalized_model=='qwen3.5-flash' or
                             normalized_model.startswith('qwen3.5-flash-')):
        if normalized['input_tokens'] is None or normalized['output_tokens'] is None:
            return {"cost_cny":None,"cost_status":"unknown_usage",
                    "price_period":"beijing_standard_before_free_quota",
                    "pricing_source":"https://help.aliyun.com/zh/model-studio/qwen3-5-flash"}
        # Alibaba's Beijing-region Qwen3.5-Flash standard tariff is tiered by
        # input length. This gross estimate deliberately ignores free quota
        # and cache discounts; it must not be mistaken for the settled bill.
        input_count=normalized['input_tokens']
        if input_count<=128_000:input_rate,output_rate=Decimal('0.2'),Decimal('2')
        elif input_count<=256_000:input_rate,output_rate=Decimal('0.8'),Decimal('8')
        else:input_rate,output_rate=Decimal('1.2'),Decimal('12')
        value=(Decimal(input_count)*input_rate+
               Decimal(normalized['output_tokens'])*output_rate)/Decimal(1_000_000)
        return {"cost_cny":float(value.quantize(Decimal('0.000001'))),
                "cost_status":"estimated_before_free_quota",
                "price_period":"beijing_standard_before_free_quota",
                "pricing_source":"https://help.aliyun.com/zh/model-studio/qwen3-5-flash"}
    if provider=='opencode' and str(model or '').lower()=='deepseek-v4.1-flash-free':
        return {"cost_cny":0.0,"cost_status":"published_temporary_free_rate",
                "price_period":"temporary_free","pricing_source":"https://opencode.ai/v2/docs/console/models/"}
    rates=_rates.get(str(model or "").lower())
    if not normalized["known"] or not rates:
        return {"cost_cny":None,"cost_status":"unknown_usage" if not normalized["known"] else "unknown_model_rate",
                "price_period":None,"pricing_source":"https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}
    peak,period,verified_period=price_window(timestamp)
    # The official Chinese tariff is quoted in CNY per million tokens.
    multiplier=Decimal(2) if peak else Decimal(1)
    hit,miss,out=(normalized[k] for k in ("cache_hit_tokens","cache_miss_tokens","output_tokens"))
    if hit is None or miss is None or out is None:
        return {"cost_cny":None,"cost_status":"unknown_usage_breakdown","price_period":period,
                "pricing_source":"https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}
    value=(Decimal(hit)*rates["hit"]+Decimal(miss)*rates["miss"]+Decimal(out)*rates["output"])*multiplier/Decimal(1_000_000)
    return {"cost_cny":float(value.quantize(Decimal("0.000001"))),
            "cost_status":"estimated_from_official_usage" if verified_period else "peak_rate_upper_bound_holiday_unverified",
            "price_period":period,"pricing_source":"https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}


def _fingerprint(messages) -> str:
    # A one-way digest supports repeat detection without persisting source text.
    digest=hashlib.sha256()
    def add(value):
        if isinstance(value,dict):
            digest.update(b"{")
            for key in sorted(value,key=lambda item:str(item)):
                add(str(key));add(value[key])
            digest.update(b"}")
        elif isinstance(value,(list,tuple)):
            digest.update(b"[")
            for item in value:add(item)
            digest.update(b"]")
        elif isinstance(value,str):
            digest.update(b"s");encoder=codecs.getincrementalencoder("utf-8")(errors="replace")
            for index in range(0,len(value),65536):digest.update(encoder.encode(value[index:index+65536]))
            digest.update(encoder.encode("",final=True));digest.update(b";")
        else:
            digest.update(type(value).__name__.encode());digest.update(b":")
            digest.update(str(value).encode("utf-8",errors="replace"));digest.update(b";")
    try:add(messages)
    except Exception:digest.update(repr(type(messages)).encode("utf-8",errors="replace"))
    return digest.hexdigest()[:24]


def record_request(*, model, messages=None, response=None, duration_ms=0, status="success",
                   error_class=None, http_status=None, retry_count=0, thinking=None,
                   request_id=None, usage=None, application_status=None, provider=None):
    ctx=context();now=datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    payload=response if response is not None else usage
    normalized=normalize_usage(payload)
    provider=(provider or '').strip().lower()
    if not provider:
        model_hint=str(model or '').strip().lower()
        provider=('qwen' if model_hint.startswith(('qwen','qwen3')) else
                  'glm' if model_hint.startswith(('glm-','glm_')) else
                  'opencode' if 'free' in model_hint and 'deepseek' in model_hint else
                  os.environ.get('PPT_STUDIO_API_PROVIDER','deepseek').strip().lower())
    # This installation is currently tracking Qwen only. Calls through other
    # providers remain fully functional, but are intentionally excluded from
    # the local Qwen cost ledger.
    if provider != 'qwen':
        return None
    served_model=(response.get("model") if isinstance(response,dict) else None)
    # OpenCode may report the underlying paid model alias for a request sent
    # with its distinct `-free` model ID. Bill/account by the requested route.
    priced_model=(str(model or '') if provider=='opencode' else (served_model or model))
    costs=calculate_cost(priced_model,now,payload or {},provider=provider)
    metrics=message_metrics(messages)
    fp=_fingerprint(messages) if messages is not None else None
    row={"request_id":request_id or uuid.uuid4().hex,"parent_request_id":ctx.get("request_id"),
         "task_id":ctx.get("task_id") or ctx.get("request_id") or "unscoped",
         "project_id":ctx.get("project_id"),"slide_id":ctx.get("slide_id"),"module":ctx.get("module","unknown"),
         "provider":provider,
         "issue_id":ctx.get("issue_id"),
         "timestamp":now,"model_requested":str(model or "unknown"),
         "model":(str(model or "unknown") if provider=='opencode' else
                  (served_model or str(model or "unknown"))),
         "model_served":served_model,
         "provider_response_id":response.get("id") if isinstance(response,dict) else None,
         "thinking_mode":thinking if thinking is not None else "omitted_default",
         "requested_reasoning_effort":"not_sent","actual_reasoning_effort":"not_exposed_by_api",
         "duration_ms":max(0,round(float(duration_ms),2)),
         "retry_count":max(0,int(retry_count or 0)),"status":status,"application_status":application_status,
         "http_status":http_status,"error_class":error_class,
         "usage":normalized,"cost":costs,"request_components":metrics,
         "input_fingerprint":fp}
    warnings=[]
    if normalized.get("input_tokens") and normalized["input_tokens"]>=_warn_token_threshold:
        warnings.append("single_request_input_tokens_high")
    if normalized.get("total_tokens") and normalized["total_tokens"]>=_warn_token_threshold:
        warnings.append("single_request_total_tokens_high")
    history=_read_recent()
    task_rows=[old for old in history if old.get("task_id")==row["task_id"]]
    if fp and sum(old.get("input_fingerprint")==fp for old in task_rows)>=1:warnings.append("same_input_repeated_in_task")
    issue_rows=[old for old in task_rows if row.get("issue_id") and old.get("issue_id")==row["issue_id"]]
    if len(issue_rows)>=2:warnings.append("same_issue_multiple_model_calls")
    if len(issue_rows)>=3:warnings.append("possible_same_issue_loop")
    old_sizes=[old.get("request_components",{}).get("content_bytes_total",0) for old in task_rows]
    if old_sizes and metrics["content_bytes_total"]>max(old_sizes[-1]*1.8, old_sizes[-1]+20_000):
        warnings.append("request_content_bytes_grew_sharply")
    if fp and sum(old.get("input_fingerprint")==fp for old in task_rows)>=2:warnings.append("possible_repeated_call_loop")
    row["anomalies"]=warnings
    _append("requests.jsonl",row)
    from .api_limits import current
    if current() is not None:current().after(row)
    return row


def record_task_outcome(*, task_id, project_id=None, slide_id=None, module=None, status="unknown",
                        repair_success=None, duration_ms=None, details=None):
    """Record benchmark outcome metadata without storing PPT/user content."""
    provider=os.environ.get('PPT_STUDIO_API_PROVIDER','deepseek').strip().lower()
    if provider!='qwen':return None
    row={"task_id":str(task_id),"project_id":project_id,"slide_id":slide_id,"module":module,
         "provider":provider,
         "timestamp":datetime.now(timezone.utc).isoformat(timespec="milliseconds"),"status":status,
         "repair_success":repair_success,"duration_ms":duration_ms,
         "details":details if isinstance(details,dict) else {}}
    _append("outcomes.jsonl",row);return row


def summary(*, project_id=None, task_id=None):
    rows=_read()
    if project_id:rows=[r for r in rows if r.get("project_id")==project_id]
    if task_id:rows=[r for r in rows if r.get("task_id")==task_id]
    totals={k:0 for k in ("input_tokens","cache_hit_tokens","cache_miss_tokens","output_tokens","reasoning_tokens","total_tokens","duration_ms","retry_count")}
    known_cost=0.0;unknown_costs=0;known_usage=0;statuses=Counter();failures=[];warn=[];unverified_price_windows=0
    slides=defaultdict(list);modules=defaultdict(list);tasks=defaultdict(list)
    by_model=defaultdict(list);by_slide_model=defaultdict(list);by_module_model=defaultdict(list)
    model_metadata={};slide_model_metadata={};module_model_metadata={}
    provider_labels={'deepseek':'DeepSeek','qwen':'通义千问','glm':'智谱 GLM','opencode':'OpenCode'}
    for row in rows:
        provider=str(row.get('provider') or '').strip().lower()
        model=str(row.get('model') or row.get('model_requested') or 'unknown').strip() or 'unknown'
        inferred=False
        if not provider:
            # Older telemetry predates the provider field. Infer only when the
            # model ID has an unambiguous vendor prefix, and keep that fact
            # visible in the UI instead of rewriting historical log entries.
            lowered=model.lower()
            provider=('deepseek' if lowered.startswith('deepseek-') else
                      'qwen' if lowered.startswith('qwen') else
                      'glm' if lowered.startswith(('glm-','glm_')) else 'unknown')
            inferred=provider!='unknown'
        provider_label=provider_labels.get(provider,provider or '未知供应商')
        model_key=f'{provider_label} / {model}'
        row['_provider_inferred_for_display']=inferred
        by_model[model_key].append(row)
        model_metadata[model_key]={'provider':provider,'provider_label':provider_label,'model':model}
        slide_key=row.get('slide_id') or '未关联页面'
        slide_model_key=f'{slide_key} / {model_key}'
        by_slide_model[slide_model_key].append(row)
        slide_model_metadata[slide_model_key]={'slide_id':slide_key,'provider':provider,
            'provider_label':provider_label,'model':model}
        module_key=row.get('module') or 'unknown'
        module_model_key=f'{module_key} / {model_key}'
        by_module_model[module_model_key].append(row)
        module_model_metadata[module_model_key]={'module':module_key,'provider':provider,
            'provider_label':provider_label,'model':model}
        use=row.get("usage") or {};known_usage+=bool(use.get("known"))
        for key in totals:
            if key!="retry_count" and use.get(key) is not None:totals[key]+=use[key]
        totals["duration_ms"]+=row.get("duration_ms",0)
        cost=row.get("cost",{}).get("cost_cny")
        if cost is None:unknown_costs+=1
        else:known_cost+=cost
        if row.get("cost",{}).get("cost_status")=="peak_rate_upper_bound_holiday_unverified":unverified_price_windows+=1
        statuses[row.get("status","unknown")]+=1
        application_status=row.get("application_status")
        if (row.get("status")!="success" or row.get("retry_count",0) or
                application_status not in (None,"success","stream_final_usage_received","stream_usage_missing")):
            failures.append(row)
        if row.get("anomalies"):warn.extend({"request_id":row["request_id"],"task_id":row["task_id"],"slide_id":row.get("slide_id"),"module":row.get("module"),"issue_id":row.get("issue_id"),"flags":row["anomalies"]} for _ in [0])
        for mapping,key in ((slides,row.get("slide_id") or "未关联页面"),(modules,row.get("module") or "unknown"),(tasks,row.get("task_id") or "unscoped")):
            mapping[key].append(row)
    def group(items,metadata=None):
        result=[]
        for key,group_rows in items.items():
            use={name:sum((r.get("usage") or {}).get(name) or 0 for r in group_rows)
                 for name in ("input_tokens","cache_hit_tokens","cache_miss_tokens","output_tokens","reasoning_tokens","total_tokens")}
            costs=[r.get("cost",{}).get("cost_cny") for r in group_rows if r.get("cost",{}).get("cost_cny") is not None]
            inferred_count=sum(bool(r.get('_provider_inferred_for_display')) for r in group_rows)
            result.append({"key":key,"requests":len(group_rows),"usage":use,"cost_cny":sum(costs),
                           "unknown_cost_requests":len(group_rows)-len(costs),"duration_ms_total":sum(r.get("duration_ms",0) for r in group_rows),
                           "duration_ms_avg":round(sum(r.get("duration_ms",0) for r in group_rows)/max(1,len(group_rows)),1),
                           "duration_ms_max":max((r.get("duration_ms",0) for r in group_rows),default=0),
                           "provider_inferred_requests":inferred_count,**((metadata or {}).get(key) or {})})
        return sorted(result,key=lambda item:item["cost_cny"],reverse=True)
    outcomes=_read("outcomes.jsonl")
    transfers=_read('transfers.jsonl')
    if project_id:transfers=[r for r in transfers if r.get('project_id')==project_id]
    if task_id:transfers=[r for r in transfers if r.get('task_id')==task_id]
    network={'upload_bytes':sum(r.get('upload_bytes',0) for r in transfers),
             'inline_bytes_avoided':sum(r.get('inline_bytes_avoided',0) for r in transfers),
             'references':sum(r.get('kind')=='image_reference' for r in transfers),
             'reused_references':sum(r.get('kind')=='image_reference' and bool(r.get('reused')) for r in transfers),
             'inline_fallbacks':sum(r.get('kind')=='inline_fallback' for r in transfers),
             'request_content_bytes':sum(r.get('request_components',{}).get('content_bytes_total',0) for r in rows)}
    if project_id:outcomes=[r for r in outcomes if r.get("project_id")==project_id]
    if task_id:outcomes=[r for r in outcomes if r.get("task_id")==task_id]
    measured=[r for r in outcomes if isinstance(r.get("repair_success"),bool)]
    measured_durations=[r.get("duration_ms") for r in outcomes if isinstance(r.get("duration_ms"),(int,float))]
    outcome_summary={"measured_runs":len(measured),"successful_runs":sum(bool(r["repair_success"]) for r in measured),
        "repair_success_rate":(sum(bool(r["repair_success"]) for r in measured)/len(measured) if measured else None),
        "duration_ms_avg":(round(sum(measured_durations)/len(measured_durations),1) if measured_durations else None),
        "duration_ms_max":(max(measured_durations) if measured_durations else None)}
    totals["retry_count"]=sum(1 for row in rows if (row.get("retry_count") or 0)>0)
    return {"request_count":len(rows),"known_usage_requests":known_usage,"unknown_cost_requests":unknown_costs,
            "unverified_price_windows":unverified_price_windows,
            "totals":totals,"cost_cny":round(known_cost,6),"cost_is_partial":unknown_costs>0,
            "status_counts":dict(statuses),"application_failure_count":sum(1 for r in failures if r.get("status")=="success"),
            "by_model":group(by_model,model_metadata),
            "by_slide_model":group(by_slide_model,slide_model_metadata),
            "by_module_model":group(by_module_model,module_model_metadata),
            "by_slide":group(slides),"by_module":group(modules),"by_task":group(tasks),
            "most_expensive":[public_row(r) for r in sorted(rows,key=lambda r:r.get("cost",{}).get("cost_cny") or -1,reverse=True)[:10]],
            "failures_and_retries":[public_row(r) for r in failures[-50:]],"anomalies":warn[-100:],
            "outcome_summary":outcome_summary,"outcomes":outcomes[-100:],"network":network}


def public_row(row: dict) -> dict:
    """Remove internal fingerprint and return safe UI/API diagnostics."""
    result={k:v for k,v in row.items() if k not in {"input_fingerprint","_provider_inferred_for_display"}}
    provider=str(result.get('provider') or '').strip().lower()
    model=str(result.get('model') or result.get('model_requested') or 'unknown').strip() or 'unknown'
    inferred=False
    if not provider:
        lowered=model.lower()
        provider=('deepseek' if lowered.startswith('deepseek-') else
                  'qwen' if lowered.startswith('qwen') else
                  'glm' if lowered.startswith(('glm-','glm_')) else 'unknown')
        inferred=provider!='unknown'
    labels={'deepseek':'DeepSeek','qwen':'通义千问','glm':'智谱 GLM','opencode':'OpenCode'}
    result['provider']=provider
    result['provider_label']=labels.get(provider,provider or '未知供应商')
    result['model_label']=f"{result['provider_label']} / {model}"
    result['provider_inferred']=inferred
    return result


class StreamingRequestTelemetry:
    """Capture the final SSE usage event; stream text is never retained."""
    def __init__(self, *, model, messages=None, thinking=None, retry_count=0):
        self.model=model;self.messages=messages;self.thinking=thinking;self.retry_count=retry_count
        self.usage=None;self.response_id=None;self.served_model=None
        self._sse_buffer=""

    def feed(self, chunk):
        if isinstance(chunk,bytes):chunk=chunk.decode("utf-8",errors="replace")
        if isinstance(chunk,str):
            self._sse_buffer+=chunk
            lines=re.split(r"\r?\n",self._sse_buffer)
            self._sse_buffer=lines.pop() if lines else ""
            for line in lines:
                line=line.strip()
                if line.startswith("data:") and line[5:].strip()!="[DONE]":
                    try:self.feed(json.loads(line[5:].strip()))
                    except (ValueError,TypeError):continue
            return
        if not isinstance(chunk,dict):return
        if isinstance(chunk.get("usage"),dict):self.usage=chunk["usage"]
        if chunk.get("id"):self.response_id=chunk["id"]
        if chunk.get("model"):self.served_model=chunk["model"]

    def finish(self, *, duration_ms, status="success", error_class=None, http_status=None):
        if self._sse_buffer.startswith("data:") and self._sse_buffer[5:].strip()!="[DONE]":
            try:self.feed(json.loads(self._sse_buffer[5:].strip()))
            except (ValueError,TypeError):pass
        self._sse_buffer=""
        response={"id":self.response_id,"model":self.served_model or self.model,"usage":self.usage} if self.usage else None
        return record_request(model=self.model,messages=self.messages,response=response,duration_ms=duration_ms,
            status=status,error_class=error_class,http_status=http_status,retry_count=self.retry_count,
            thinking=self.thinking,application_status="stream_final_usage_received" if self.usage else "stream_usage_missing")
