; RUN: rm -f %t.manifest %t.seal
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=BIG
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); s=r['memory_slots'][0]; x=next(x for x in s['exit_states'] if x['state_kind']=='symbolic-region-writer-graph'); w=x['writer_layers'][0]; assert x['endianness']=='big' and w['region_extent']=='4' and [[(c['index_value'],c['source_byte']) for c in lane['cases']] for lane in w['lane_cases']]==[[(0,0),(-1,1)],[(1,0),(0,1)]]"
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.ll --compiler %passlib --llvm-tool %opt

target datalayout = "E-m:e-i64:64-n32:64"
target triple = "powerpc64-unknown-linux-gnu"

define i16 @big_endian_symbolic_region(i8 %selector, i64 %raw_index) {
entry:
  %heap = call ptr @malloc(i64 4)
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  store i16 4386, ptr %heap, align 2
  %index = and i64 %raw_index, 2
  %dynamic = getelementptr inbounds i8, ptr %heap, i64 %index
  store i16 13124, ptr %dynamic, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %heap, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %heap, align 2
  ret i16 %state

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

declare noalias ptr @malloc(i64)

; BIG-LABEL: define i16 @big_endian_symbolic_region
; BIG: %ifss.cont.byte{{[0-9]+}} = trunc i16 13124 to i8
; BIG: %ifss.cont.region.hit = icmp eq i64 %index, -1
; BIG: %ifss.cont.byte.region = select i1 %ifss.cont.region.hit
; BIG: %ifss.cont.byte.extract{{[0-9]+}} = lshr i16 13124, 8
; BIG: %ifss.cont.memory = phi i16
; BIG: symbolic-region-writer-graph-continuation-memory-tuple-v16
