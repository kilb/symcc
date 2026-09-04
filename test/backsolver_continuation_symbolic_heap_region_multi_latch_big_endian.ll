; RUN: rm -f %t.manifest %t.seal
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=BIG
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='symbolic-region-multi-latch-cyclic-byte-composition'); b=c['backedge_states']; w=[x['symbolic_region_writer'] for x in b]; kinds=[x['transfer_kind'] for x in b]; assert r['analysis']=='llvm-memoryssa-aa-symbolic-region-multi-latch-cyclic-byte-lane-revalidated-v18' and c['endianness']=='big' and len(b)==4 and kinds.count('conditional')==1 and kinds.count('unconditional')==3 and all(x['store_width']==2 for x in w) and [[(y['index_value'],y['source_byte']) for y in x['lane_cases'][0]['cases']] for x in w]==[[(0,0),(-1,1)],[(0,0),(-1,1)],[(0,0),(-1,1)],[(0,0),(-1,1)]]"
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt

target datalayout = "E-m:e-i64:64-n32:64"
target triple = "powerpc64-unknown-linux-gnu"

define i32 @choose_loop_big(
    i1 %take, i8 %iterations, i1 %write) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i32 287454020, ptr %slot, align 4
  %limit = zext i8 %iterations to i64
  %write.guard = xor i1 %write, true
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ %next.0, %latch.0 ],
                   [ %next.1, %latch.1 ], [ %next.2, %latch.2 ],
                   [ %next.3, %latch.3 ]
  %done = icmp uge i64 %index, %limit
  br i1 %done, label %loop.exit, label %body

body:
  %arm = and i64 %index, 3
  switch i64 %arm, label %write.3 [
    i64 0, label %branch.0
    i64 1, label %write.1
    i64 2, label %write.2
  ]

branch.0:
  br i1 %write.guard, label %write.0, label %carry.0

write.0:
  %ptr.0 = getelementptr inbounds i8, ptr %slot, i64 %index
  store i16 4386, ptr %ptr.0, align 1
  br label %latch.0

carry.0:
  br label %latch.0

latch.0:
  %next.0 = add i64 %index, 1
  br label %loop

write.1:
  %index.1 = add i64 %index, 1
  %ptr.1 = getelementptr inbounds i8, ptr %slot, i64 %index.1
  store i16 13124, ptr %ptr.1, align 1
  br label %latch.1

latch.1:
  %next.1 = add i64 %index, 1
  br label %loop

write.2:
  %index.2 = add i64 %index, 2
  %ptr.2 = getelementptr inbounds i8, ptr %slot, i64 %index.2
  store i16 21862, ptr %ptr.2, align 1
  br label %latch.2

latch.2:
  %next.2 = add i64 %index, 1
  br label %loop

write.3:
  %index.3 = add i64 %index, 3
  %ptr.3 = getelementptr inbounds i8, ptr %slot, i64 %index.3
  store i16 30600, ptr %ptr.3, align 1
  br label %latch.3

latch.3:
  %next.3 = add i64 %index, 1
  br label %loop

loop.exit:
  br i1 %take, label %left, label %right

left:
  br label %continuation_a

right:
  store i32 1432778632, ptr %slot, align 4
  br label %continuation_b

continuation_a:
  %tag_a = phi i32 [ 1, %left ]
  %state = load i32, ptr %slot, align 4
  %result_a = xor i32 %state, %tag_a
  ret i32 %result_a

continuation_b:
  %tag_b = phi i32 [ 2, %right ]
  ret i32 %tag_b
}

declare noalias ptr @malloc(i64)

; BIG-LABEL: define i32 @choose_loop_big
; BIG-LABEL: loop:
; BIG: %ifss.cont.memory.multi.cycle = phi i32
; BIG-LABEL: latch.0:
; BIG: icmp eq i64 %index, -1
; BIG: %ifss.cont.byte.region.guard = select i1 %write.guard
; BIG-LABEL: latch.1:
; BIG: icmp eq i64 %index.1, -1
; BIG-LABEL: latch.2:
; BIG: icmp eq i64 %index.2, -1
; BIG-LABEL: latch.3:
; BIG: icmp eq i64 %index.3, -1
; BIG: symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18
