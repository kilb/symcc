; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=CALLOC
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); s=r['memory_slots'][0]; x=next(x for x in s['exit_states'] if x['state_kind']=='symbolic-region-writer-graph'); w=x['writer_layers'][0]; assert r['analysis']=='llvm-memoryssa-aa-symbolic-region-writer-graph-revalidated-v16' and w['kind']=='symbolic-region-write' and w['region_extent']=='8' and w['base_offset']==2 and [[c['index_value'] for c in lane['cases']] for lane in w['lane_cases']]==[[0],[1]]"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i16 @calloc_window(i8 %selector, i64 %index) {
entry:
  %heap = call ptr @calloc(i64 4, i64 2)
  %window = getelementptr inbounds i8, ptr %heap, i64 2
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  store i16 4386, ptr %window, align 2
  %stable.index = freeze i64 %index
  %dynamic = getelementptr inbounds i8, ptr %window, i64 %stable.index
  store i8 51, ptr %dynamic, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %window, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %window, align 2
  ret i16 %state

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

declare noalias ptr @calloc(i64, i64)

; CALLOC-LABEL: define i16 @calloc_window
; CALLOC: %ifss.cont.region.hit = icmp eq i64 %stable.index, 0
; CALLOC: %ifss.cont.byte.region = select i1 %ifss.cont.region.hit
; CALLOC: %ifss.cont.memory = phi i16
; CALLOC: symbolic-region-writer-graph-continuation-memory-tuple-v16
