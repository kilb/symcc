; RUN: rm -f %t.manifest %t.seal
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=BIG
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='conditional-multi-latch-cyclic-byte-composition'); b=c['backedge_states']; assert r['analysis']=='llvm-memoryssa-aa-conditional-multi-latch-cyclic-byte-lane-revalidated-v12' and s['state_schema']=='conditional-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v12' and c['endianness']=='big' and [x['transfer_kind'] for x in b]==['conditional','conditional'] and [x['cycle_store_when'] for x in b]==['true','false'] and b[0]['cycle_guard_site']==b[1]['cycle_guard_site'] and [[y['source_kind'] for y in x['lanes']] for x in b]==[['carry','guarded-store'],['guarded-store','carry']]"
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt

target datalayout = "E-m:e-i64:64-n32:64"
target triple = "powerpc64-unknown-linux-gnu"

define i16 @choose_loop_big(
    i8 %selector, i8 %iterations, i8 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %next.low, %latch.low ],
                  [ %next.high, %latch.high ]
  %done = icmp uge i8 %index, %iterations
  br i1 %done, label %loop.exit, label %body

body:
  %parity = and i8 %index, 1
  %use.low = icmp eq i8 %parity, 0
  %shared.guard = icmp ne i8 %write, 0
  br i1 %use.low, label %low.branch, label %high.branch

low.branch:
  br i1 %shared.guard, label %low.write, label %low.carry

low.write:
  %low = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 51, ptr %low, align 1
  br label %latch.low

low.carry:
  br label %latch.low

latch.low:
  %next.low = add i8 %index, 1
  br label %loop

high.branch:
  br i1 %shared.guard, label %high.carry, label %high.write

high.write:
  store i8 68, ptr %slot, align 1
  br label %latch.high

high.carry:
  br label %latch.high

latch.high:
  %next.high = add i8 %index, 1
  br label %loop

loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
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
  %tag_a = phi i16 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %result_a = add i16 %state, %tag_a
  ret i16 %result_a

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

; BIG-LABEL: define i16 @choose_loop_big
; BIG-LABEL: loop:
; BIG: %ifss.cont.memory.multi.cycle = phi i16
; BIG-LABEL: latch.low:
; BIG: %ifss.cont.byte.cycle.guard = select i1 %shared.guard
; BIG-LABEL: latch.high:
; BIG: %ifss.cont.byte.cycle.guard{{[0-9]+}} = select i1 %shared.guard
; BIG: conditional-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v12
