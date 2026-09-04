; RUN: rm -f %t.manifest %t.seal
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=BIG
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); s=r['memory_slots'][0]; x=next(x for x in s['exit_states'] if x['state_kind']=='ordered-writer-graph'); w=x['writer_layers']; assert r['analysis']=='llvm-memoryssa-aa-ordered-writer-graph-revalidated-v15' and s['state_schema']=='ordered-writer-graph-continuation-memory-tuple-v15' and x['endianness']=='big' and [y['kind'] for y in w]==['pointer-partition','pointer-partition'] and all(any(z['lane_source_bytes'][0]==0 for z in y['pointer_partition']['leaves']) for y in w)"
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt

target datalayout = "E-m:e-i64:64-n32:64"
target triple = "powerpc64-unknown-linux-gnu"

define i8 @choose_two_partitions(
    i8 %selector, i8 %o0, i8 %o1, i8 %n0, i8 %n1, ptr %slot) {
entry:
  %other.o0 = alloca i8, align 1
  %other.o1 = alloca i8, align 1
  %other.n0 = alloca i8, align 1
  %other.n1 = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i16 4386, ptr %slot, align 2
  %old.g0 = icmp eq i8 %o0, 0
  %old.g1 = icmp eq i8 %o1, 0
  %old.t = select i1 %old.g1, ptr %slot, ptr %other.o0
  %old.f = select i1 %old.g1, ptr %other.o1, ptr %slot
  %old.pointer = select i1 %old.g0, ptr %old.t, ptr %old.f
  store i8 51, ptr %old.pointer, align 1
  %new.g0 = icmp eq i8 %n0, 0
  %new.g1 = icmp eq i8 %n1, 0
  %new.t = select i1 %new.g1, ptr %slot, ptr %other.n0
  %new.f = select i1 %new.g1, ptr %other.n1, ptr %slot
  %new.pointer = select i1 %new.g0, ptr %new.t, ptr %new.f
  store i8 68, ptr %new.pointer, align 1
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep

middle:
  store i16 17493, ptr %slot, align 2
  br label %continuation_a

deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i8 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %high.word = lshr i16 %state, 8
  %high.byte = trunc i16 %high.word to i8
  %low.byte = trunc i16 %state to i8
  %fold = xor i8 %high.byte, %low.byte
  %result_a = add i8 %fold, %tag_a
  br label %final

continuation_b:
  %tag_b = phi i8 [ 3, %deep ]
  br label %final

final:
  %result = phi i8 [ %result_a, %continuation_a ],
                   [ %tag_b, %continuation_b ]
  ret i8 %result
}

; BIG-LABEL: define i8 @choose_two_partitions
; BIG: %ifss.cont.byte.partition
; BIG: %ifss.cont.byte.partition
; BIG: %ifss.cont.memory = phi i16
; BIG: ordered-writer-graph-continuation-memory-tuple-v15
