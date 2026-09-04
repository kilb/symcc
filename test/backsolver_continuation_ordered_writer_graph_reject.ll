; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=EXTENDED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); s=r['memory_slots'][0]; x=s['exit_states'][0]; assert r['analysis']=='llvm-memoryssa-aa-symbolic-region-writer-graph-revalidated-v16' and s['state_schema']=='symbolic-region-writer-graph-continuation-memory-tuple-v16' and x['state_kind']=='symbolic-region-writer-graph' and [w['kind'] for w in x['writer_layers']]==['pointer-partition']*3"

define i16 @three_partition_writers(
    i8 %selector, i8 %a0, i8 %a1, i8 %b0, i8 %b1,
    i8 %c0, i8 %c1, ptr %slot) {
entry:
  %other.a0 = alloca i8, align 1
  %other.a1 = alloca i8, align 1
  %other.b0 = alloca i8, align 1
  %other.b1 = alloca i8, align 1
  %other.c0 = alloca i8, align 1
  %other.c1 = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %ag0 = icmp eq i8 %a0, 0
  %ag1 = icmp eq i8 %a1, 0
  %at = select i1 %ag1, ptr %high, ptr %other.a0
  %af = select i1 %ag1, ptr %other.a1, ptr %high
  %ap = select i1 %ag0, ptr %at, ptr %af
  store i8 51, ptr %ap, align 1
  %bg0 = icmp eq i8 %b0, 0
  %bg1 = icmp eq i8 %b1, 0
  %bt = select i1 %bg1, ptr %high, ptr %other.b0
  %bf = select i1 %bg1, ptr %other.b1, ptr %high
  %bp = select i1 %bg0, ptr %bt, ptr %bf
  store i8 68, ptr %bp, align 1
  %cg0 = icmp eq i8 %c0, 0
  %cg1 = icmp eq i8 %c1, 0
  %ct = select i1 %cg1, ptr %high, ptr %other.c0
  %cf = select i1 %cg1, ptr %other.c1, ptr %high
  %cp = select i1 %cg0, ptr %ct, ptr %cf
  store i8 85, ptr %cp, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

; EXTENDED-LABEL: define i16 @three_partition_writers
; EXTENDED: %ifss.cont.byte.partition
; EXTENDED: %ifss.cont.memory = phi i16
; EXTENDED: %state = load i16, ptr %slot
; EXTENDED: ret i16 %ifss.cont.memory
; EXTENDED: symbolic-region-writer-graph-continuation-memory-tuple-v16
