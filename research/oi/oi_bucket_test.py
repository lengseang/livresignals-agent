import sys, statistics
sys.path.insert(0,"/opt/trading-agent/src")
exec(open('/tmp/bt_oi_veto.py').read().replace('if __name__ == "__main__":\n    main()',''))
closes=load_daily(); oi=load_oi(); days=align(closes,oi)
eq,trades=run(closes,oi,days,False)
# winsorize at +/-20% and also median
def stat(v):
    if not v: return "n=0"
    wr=sum(1 for x in v if x>0)/len(v)
    return f"n={len(v):>3} WR={wr:5.1%} mean={statistics.mean(v):+6.2%} median={statistics.median(v):+6.2%}"
import datetime as dt
sq={"squeeze":[], "clean":[]}
sq_w={"squeeze":[], "clean":[]}
for t in trades:
    isq=squeeze_at(days,t[0],oi); p=t[2]
    sq["squeeze" if isq else "clean"].append(p)
    sq_w["squeeze" if isq else "clean"].append(max(-0.20,min(0.20,p)))
print("RAW (outliers included):")
for k in sq: print(f"  {k:8} {stat(sq[k])}")
print("WINSORIZED ±20%:")
for k in sq_w: print(f"  {k:8} {stat(sq_w[k])}")
# fold split of the squeeze/clean separation
split=205+int((len(days)-205)*0.6)
print(f"\nFold split (IS< {split}):")
for name,lo,hi in [("IS",0,split),("OOS",split,len(days))]:
    b={"squeeze":[],"clean":[]}
    for t in trades:
        if lo<=t[0]<hi:
            b["squeeze" if squeeze_at(days,t[0],oi) else "clean"].append(t[2])
    for k in b: print(f"  {name} {k:8} {stat(b[k])}")
