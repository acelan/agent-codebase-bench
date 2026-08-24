import re,sys
SIG=re.compile(r'^\s*(?:static\s+)?[A-Za-z_][\w\s\*&<>]*?\b(\w+)\s*\([^;]*\)\s*$')
STARTSIG=re.compile(r'^\s*(?:static\s+)?(?:[\w<>\[\]\*&\s]+?)\b(\w+)\s*\([^;]*$')
def enclosing(path,target):
    lines=open('/workspace/linux/drivers/gpu/drm/i915/'+path).read().split('\n')
    depth=0; func=None; pend=None
    for i,l in enumerate(lines,1):
        s=l.strip()
        # detect open brace of a signature
        # simpler: track when we see a '{' that follows a prior function signature
        for m in re.finditer(r'\{', l):
            if pend is not None:
                func=pend; pend=None
        if pend is None:
            # try to detect start of a signature ending on this or next line
            m=STARTSIG.match(l)
            if m and '{' not in l[:l.find('(') if '(' in l else 999] and not s.startswith(('/','*','//')):
                pend=m.group(1)
        depth += l.count('{')-l.count('}')
        if i==target:
            print(f"{path}:{target} -> enclosing func: {func}")
            return
    print(f"{path}:{target} -> not found")
for e in sys.argv[1:]:
    f,t=e.rsplit(':',1); enclosing(f,int(t))