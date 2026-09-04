; RUN: rm -f %t.recurrence %t.exits
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 SYMCC_IFSS_LOOP_MANIFEST_OUT=%t.recurrence SYMCC_IFSS_LOOP_EXIT_MANIFEST_OUT=%t.exits %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=SUMMARY
; RUN: %python %S/../util/verify_ifss_loop_recurrence_manifest.py %t.recurrence
; RUN: %python %S/../util/verify_ifss_loop_exit_manifest.py %t.exits
; RUN: %python -c "import json; r=json.loads(open(r'%t.exits').read()); assert r['break_count']==3 and r['state_count']==2 and r['recurrence_kind']=='upper-triangular-v1' and len(r['execution_table'])==256"
; RUN: rm -f %t.unified.seal %t.lowered-tamper.seal
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline loop --manifest loop-recurrence=%t.recurrence --manifest loop-exit=%t.exits --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/seal_transform_artifact.py verify --pipeline loop --manifest loop-recurrence=%t.recurrence --manifest loop-exit=%t.exits --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline loop --manifest loop-recurrence=%t.recurrence --manifest loop-exit=%t.exits --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old=' = add i8 '; assert p.count(old)>=1; open(r'%t.lowered-tamper.ll','w').write(p.replace(old,' = sub i8 ',1))"
; RUN: %opt -passes=verify -disable-output %t.lowered-tamper.ll
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline loop --manifest loop-recurrence=%t.recurrence --manifest loop-exit=%t.exits --input-ir %s --lowered-ir %t.lowered-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.lowered-tamper.seal
; RUN: not %python %S/../util/replay_transform_artifact.py --pipeline loop --manifest loop-recurrence=%t.recurrence --manifest loop-exit=%t.exits --input-ir %s --lowered-ir %t.lowered-tamper.ll --compiler %passlib --llvm-tool %opt --seal %t.lowered-tamper.seal
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=0 %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.disabled.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,b,c,d: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,b,c,d]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.disabled.ll',a,b,c,d)==run(r'%t.lowered.ll',a,b,c,d) for a in range(4) for b in range(4) for c in range(4) for d in range(4))"
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @three_break_triangular(
    i8 %raw_trip, i8 %raw_break0, i8 %raw_break1, i8 %raw_break2) {
entry:
  %trip = and i8 %raw_trip, 3
  %break_at0 = and i8 %raw_break0, 3
  %break_at1 = and i8 %raw_break1, 3
  %break_at2 = and i8 %raw_break2, 3
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %check2 ]
  %x = phi i8 [ 1, %entry ], [ %x_next, %check2 ]
  %y = phi i8 [ 2, %entry ], [ %y_next, %check2 ]
  %continue = icmp ult i8 %index, %trip
  br i1 %continue, label %update, label %normal

update:
  %x_next = add i8 %x, %y
  %y_next = add i8 %y, 1
  %index_next = add i8 %index, 1
  br label %check0

check0:
  %should_break0 = icmp eq i8 %index, %break_at0
  br i1 %should_break0, label %break0, label %check1

check1:
  %should_break1 = icmp eq i8 %index, %break_at1
  br i1 %should_break1, label %break1, label %check2

check2:
  %should_break2 = icmp eq i8 %index, %break_at2
  br i1 %should_break2, label %break2, label %loop

normal:
  %normal_x = phi i8 [ %x, %loop ]
  br label %final

break0:
  %break_x0 = phi i8 [ %x_next, %check0 ]
  br label %final

break1:
  %break_x1 = phi i8 [ %x_next, %check1 ]
  br label %final

break2:
  %break_x2 = phi i8 [ %x_next, %check2 ]
  br label %final

final:
  %x_out = phi i8 [ %normal_x, %normal ],
                  [ %break_x0, %break0 ],
                  [ %break_x1, %break1 ],
                  [ %break_x2, %break2 ]
  %bias = phi i8 [ 0, %normal ], [ 40, %break0 ],
                 [ 80, %break1 ], [ 120, %break2 ]
  %result = add i8 %x_out, %bias
  ret i8 %result
}

define i32 @semantic_main() {
entry:
  %input = alloca [4 x i8], align 1
  %pointer = getelementptr [4 x i8], ptr %input, i64 0, i64 0
  %read = call i64 @read(i32 0, ptr %pointer, i64 4)
  %raw_trip = load i8, ptr %pointer, align 1
  %p1 = getelementptr i8, ptr %pointer, i64 1
  %raw_break0 = load i8, ptr %p1, align 1
  %p2 = getelementptr i8, ptr %pointer, i64 2
  %raw_break1 = load i8, ptr %p2, align 1
  %p3 = getelementptr i8, ptr %pointer, i64 3
  %raw_break2 = load i8, ptr %p3, align 1
  %result = call i8 @three_break_triangular(
      i8 %raw_trip, i8 %raw_break0, i8 %raw_break1, i8 %raw_break2)
  %status = zext i8 %result to i32
  ret i32 %status
}

declare i64 @read(i32, ptr, i64)

; SUMMARY-LABEL: define i8 @three_break_triangular
; SUMMARY: %ifss.loop.break_better
; SUMMARY: %ifss.loop.break_better{{[0-9]+}}
; SUMMARY: %ifss.loop.break_better{{[0-9]+}}
; SUMMARY: %ifss.loop.executions = select
; SUMMARY: %ifss.loop.binomial
; SUMMARY: br label %ifss.loop.break.dispatch
; SUMMARY-LABEL: ifss.loop.break.dispatch:
; SUMMARY-LABEL: ifss.loop.break.dispatch{{[0-9]+}}:
; SUMMARY-LABEL: ifss.loop.break.dispatch{{[0-9]+}}:
; SUMMARY-NOT: loop:
; SUMMARY: !"bounded-multi-break-loop-exit-v2"

; IFSS-LABEL: define i8 @three_break_triangular
; IFSS-NOT: loop:
; IFSS: call ptr @_sym_build_unsigned_less_than
; IFSS: call ptr @_sym_build_ite
; IFSS: !"bounded-multi-break-loop-exit-v2"
