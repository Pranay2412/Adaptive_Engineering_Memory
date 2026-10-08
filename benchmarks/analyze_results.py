import json
from collections import defaultdict

with open('benchmarks/results/budget_benchmark_results.json', 'r') as f:
    data = json.load(f)

trials = data['trials']
print(f"Total trials analyzed: {len(trials)}")

by_mode = defaultdict(lambda: defaultdict(list))
for t in trials:
    b = str(t['budget'])
    m = t['optimization_mode']
    by_mode[m][b].append(t)

print("\n" + "="*90)
print(f"{'Mode':<15} | {'Budget':<10} | {'Ctx Tok':<9} | {'Prompt Tok':<10} | {'Out Tok':<9} | {'Total Tok':<10} | {'Util %':<9} | {'Quality':<7} | {'Latency (ms)':<12}")
print("="*90)

for m in ['deterministic', 'none', 'llm']:
    if m not in by_mode:
        continue
    for b in ['500', '1000', '1500', '2500', '4000', 'None']:
        if b not in by_mode[m]:
            continue
        ts = by_mode[m][b]
        ctx = sum(x['final_context_tokens'] for x in ts) / len(ts)
        prompt = sum(x['actual_provider_prompt_tokens'] for x in ts) / len(ts)
        out = sum(x['output_completion_tokens'] for x in ts) / len(ts)
        total = sum(x['total_tokens'] for x in ts) / len(ts)
        def get_qual(x):
            q = x['quality_score']
            if isinstance(q, dict):
                return q.get('overall_score', 0.0)
            return float(q)
        qual = sum(get_qual(x) for x in ts) / len(ts)
        lat = sum(x['total_latency_ms'] for x in ts) / len(ts)
        util_vals = [x['budget_utilization_pct'] for x in ts if x['budget_utilization_pct'] is not None]
        util_str = f"{sum(util_vals)/len(util_vals):.1f}%" if util_vals else "N/A"
        b_label = "unbounded" if b == "None" else b
        print(f"{m:<15} | {b_label:<10} | {ctx:<9.1f} | {prompt:<10.1f} | {out:<9.1f} | {total:<10.1f} | {util_str:<9} | {qual:<7.2f} | {lat:<12.1f}")
    print("-" * 90)

print("\n" + "="*90)
print("PER-QUERY BREAKDOWN (Deterministic Mode):")
print(f"{'Category':<15} | {'Budget':<10} | {'Ctx Tok':<8} | {'Prompt Tok':<10} | {'Total Tok':<10} | {'Quality':<7} | {'Latency':<8}")
print("="*90)
for t in by_mode['deterministic']['1500']:
    q_val = t['quality_score']['overall_score'] if isinstance(t['quality_score'], dict) else float(t['quality_score'])
    print(f"{t['category']:<15} | {'1500':<10} | {t['final_context_tokens']:<8} | {t['actual_provider_prompt_tokens']:<10} | {t['total_tokens']:<10} | {q_val:<7.2f} | {t['total_latency_ms']:<8.0f}")

print("\n" + "="*90)
print("CANDIDATE PRUNING & SELECTION (Deterministic Mode):")
print(f"{'Budget':<10} | {'Raw Retrieved':<14} | {'Ranked':<8} | {'Retained':<10} | {'Removed':<8} | {'Est Ctx':<8} | {'Tiktoken Ctx':<12}")
print("="*90)
for b in ['500', '1000', '1500', '2500', '4000', 'None']:
    ts = by_mode['deterministic'][b]
    raw = sum(x['raw_candidate_count'] for x in ts) / len(ts)
    ranked = sum(x['ranked_candidate_count'] for x in ts) / len(ts)
    retained = sum(x['retained_candidate_count'] for x in ts) / len(ts)
    removed = sum(x['removed_candidate_count'] for x in ts) / len(ts)
    est = sum(x['estimated_context_tokens'] for x in ts) / len(ts)
    tik = sum(x['precise_tiktoken_context_tokens'] for x in ts) / len(ts)
    b_label = "unbounded" if b == "None" else b
    print(f"{b_label:<10} | {raw:<14.1f} | {ranked:<8.1f} | {retained:<10.1f} | {removed:<8.1f} | {est:<8.1f} | {tik:<12.1f}")
