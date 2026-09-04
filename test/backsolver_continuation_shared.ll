; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=SHARED

define i8 @same_value_edges(i1 %root, i1 %inner) {
entry:
  br i1 %root, label %shared_source, label %right

shared_source:
  br i1 %inner, label %continuation_a, label %continuation_a

right:
  br label %continuation_b

continuation_a:
  %a = phi i8 [ 11, %shared_source ], [ 11, %shared_source ]
  ret i8 %a

continuation_b:
  %b = phi i8 [ 22, %right ]
  ret i8 %b
}

; SHARED-LABEL: define i8 @same_value_edges
; SHARED-LABEL: shared_source:
; SHARED: br i1 %inner, label %ifss.cont.capture, label %ifss.cont.capture{{[0-9]+}}
; SHARED-LABEL: ifss.cont.dispatch:
; SHARED: %ifss.cont.exit_id = phi i8 [ 0, %ifss.cont.capture ], [ 1, %ifss.cont.capture{{[0-9]+}} ], [ 2, %ifss.cont.capture{{[0-9]+}} ]
; SHARED: %ifss.cont.liveout = phi i8 [ 11, %ifss.cont.capture ], [ 11, %ifss.cont.capture{{[0-9]+}} ], [ 0, %ifss.cont.capture{{[0-9]+}} ]
; SHARED-LABEL: continuation_a:
; SHARED: %a = phi i8 [ %ifss.cont.liveout, %ifss.cont.resume ], [ %ifss.cont.liveout, %ifss.cont.resume{{[0-9]+}} ]{{.*}}!symcc.ifss_continuation_liveout
; SHARED: !"bounded-continuation-tuple-v1", i64 {{-?[0-9]+}}, i32 3, i32 2, i32 2, i32 2, i32 3
