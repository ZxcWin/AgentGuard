"""Reproducible local microbenchmark; reports observations, sets no production SLA."""
import argparse
import json
import platform
import statistics
import sys
from time import perf_counter_ns
import tracemalloc

from agentguard import AgentGuard, AgentGuardConfig
from agentguard.domain.models import RunState
from agentguard.policies.budget import BudgetManager


def measure(function, count):
    samples = []
    for _ in range(count):
        start = perf_counter_ns()
        function()
        samples.append(perf_counter_ns()-start)
    samples.sort()
    return {"count":count, "median_us":round(statistics.median(samples)/1000,3),
            "p95_us":round(samples[min(count-1,int(count*.95))]/1000,3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations",type=int,default=1000)
    args = parser.parse_args()
    if not 10 <= args.iterations <= 100000: parser.error("iterations must be 10..100000")
    manager = BudgetManager(AgentGuardConfig(),RunState())
    def reserve(): manager.reserve(10,20).release()
    guard = AgentGuard()
    def provider(**kwargs): return {"content":"ok","usage":{"input_tokens":2,"output_tokens":3}}
    def invoke():
        with guard.run() as run: run.invoke(provider,max_tokens=10)
    try:
        tracemalloc.start()
        output = {"python":sys.version.split()[0],"platform":platform.platform(),
                  "reservation":measure(reserve,args.iterations),"run_and_call":measure(invoke,args.iterations)}
        current,peak = tracemalloc.get_traced_memory()
        output["traced_memory_bytes"] = {"current":current,"peak":peak}
        print(json.dumps(output,indent=2))
    finally:
        tracemalloc.stop()
        guard.close()


if __name__ == "__main__": main()
