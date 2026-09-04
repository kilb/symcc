; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=BIG
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); c=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='conditional-cyclic-byte-composition'); assert r['analysis']=='llvm-memoryssa-aa-conditional-cyclic-byte-lane-revalidated-v9' and c['endianness']=='big' and c['cycle_store_when']=='false' and [x['source_kind'] for x in c['backedge_state']['lanes']]==['carry','guarded-store']"

target datalayout = "E-m:e-i64:64-n32:64"
target triple = "powerpc64-unknown-linux-gnu"

define i16 @choose_loop_big(
    i8 %selector, i8 %iterations, i8 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %next, %latch ]
  %done = icmp uge i8 %index, %iterations
  br i1 %done, label %controller, label %body

body:
  %next = add i8 %index, 1
  %carry.guard = icmp eq i8 %write, 0
  br i1 %carry.guard, label %carry.arm, label %write.arm

write.arm:
  %low = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 51, ptr %low, align 1
  br label %latch

carry.arm:
  br label %latch

latch:
  br label %loop

controller:
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
  %result = add i16 %state, %tag_a
  ret i16 %result

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

; BIG-LABEL: define i16 @choose_loop_big
; BIG-LABEL: loop:
; BIG: %ifss.cont.memory.cycle = phi i16
; BIG-LABEL: latch:
; BIG: %ifss.cont.byte.extract{{[0-9]*}} = lshr i16 %ifss.cont.memory.cycle, 8
; BIG: %ifss.cont.byte{{[0-9]*}} = trunc i16 %ifss.cont.byte.extract{{[0-9]*}} to i8
; BIG: %ifss.cont.byte.cycle.guard = select i1 %carry.guard, i8 %ifss.cont.byte{{[0-9]*}}, i8 51
; BIG: conditional-cyclic-byte-lane-continuation-memory-tuple-v9
