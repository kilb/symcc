; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); c=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='symbolic-region-cyclic-byte-composition'); w=c['backedge_state']['symbolic_region_writer']; assert c['endianness']=='big' and w['store_width']==2 and [[(y['index_value'],y['source_byte']) for y in x['cases']] for x in w['lane_cases']]==[[(0,0),(-1,1)],[(1,0),(0,1)],[(2,0),(1,1)],[(3,0),(2,1)]]"
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=BIG

target datalayout = "E-m:e-Fn32-i64:64-n32:64-S128"
target triple = "powerpc64-unknown-linux-gnu"

define i32 @big_endian_cycle(i1 %take, i64 %iterations, i16 %value) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i32 287454020, ptr %slot, align 4
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body

body:
  %element = getelementptr inbounds i8, ptr %slot, i64 %index
  store i16 %value, ptr %element, align 1
  %next = add i64 %index, 1
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
  br label %final

continuation_b:
  %tag_b = phi i32 [ 2, %right ]
  br label %final

final:
  %result = phi i32 [ %result_a, %continuation_a ],
                    [ %tag_b, %continuation_b ]
  ret i32 %result
}

declare noalias ptr @malloc(i64)

; BIG-LABEL: define i32 @big_endian_cycle
; BIG-LABEL: body:
; BIG: %ifss.cont.region.hit{{[0-9]*}} = icmp eq i64 %index, -1
; BIG: %ifss.cont.byte.extract{{[0-9]*}} = lshr i16 %value, 8
; BIG: %ifss.cont.byte.region
; BIG: symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v17
