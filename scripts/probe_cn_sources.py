"""Small read-only source probes; response evidence stays under data/."""
import json
import os
import subprocess
import sys
from pathlib import Path

PROBES = [
    ("stock_info_a_code_name", {}),
    ("stock_zh_a_hist_tx", {"symbol":"sz000001","start_date":"20260731","end_date":"20260930","timeout":10}),
    ("fund_etf_hist_sina", {"symbol":"sh510300"}),
    ("stock_board_industry_name_ths", {}),
    ("stock_board_concept_name_ths", {}),
    ("fund_etf_category_sina", {"symbol":"ETF基金"}),
    ("stock_board_industry_index_ths", {"symbol":"半导体","start_date":"20260731","end_date":"20260930"}),
    ("stock_board_concept_index_ths", {"symbol":"人工智能","start_date":"20260731","end_date":"20260930"}),
    ("stock_zh_a_hist_tx", {"symbol":"sh688981","start_date":"20260925","end_date":"20260929","timeout":10}),
    ("stock_zh_a_hist_tx", {"symbol":"sz300750","start_date":"20260925","end_date":"20260929","timeout":10}),
    ("stock_zh_a_hist_tx", {"symbol":"bj920779","start_date":"20260925","end_date":"20260929","timeout":10}),
    ("stock_zh_a_daily", {"symbol":"sz000001","start_date":"20260731","end_date":"20260929","adjust":""}),
    ("stock_zh_a_daily", {"symbol":"bj920779","start_date":"20260731","end_date":"20260929","adjust":""}),
]


def main():
    folder=Path("data/cn-repair-probes")
    folder.mkdir(parents=True,exist_ok=True)
    if len(sys.argv)>1:
        import requests
        original=requests.sessions.Session.__init__
        def direct(self,*args,**kwargs):
            original(self,*args,**kwargs)
            self.trust_env=False
        requests.sessions.Session.__init__=direct
        import akshare as ak
        name,kwargs=PROBES[int(sys.argv[1])]
        frame=getattr(ak,name)(**kwargs)
        suffix = "-"+kwargs["symbol"] if int(sys.argv[1]) >= 8 else ""
        (folder/(name+suffix+".json")).write_text(frame.to_json(orient="records",date_format="iso",force_ascii=False),encoding="utf-8")
        print(json.dumps({"endpoint":name,"rows":len(frame),"columns":list(frame.columns)},ensure_ascii=True))
        return
    for index,(name,_) in enumerate(PROBES):
        try:
            result=subprocess.run([sys.executable,"-m","scripts.probe_cn_sources",str(index)],capture_output=True,timeout=35)
            print(result.stdout.decode("utf-8",errors="replace") if result.returncode==0 else json.dumps({"endpoint":name,"failed":True,"tail":result.stderr.decode(errors="replace")[-180:]}),flush=True)
        except subprocess.TimeoutExpired:
            print(json.dumps({"endpoint":name,"timeout":True}),flush=True)


if __name__=="__main__":
    main()
