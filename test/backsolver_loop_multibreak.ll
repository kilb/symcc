; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 SYMCC_IFSS_LOOP_EXIT_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=SUMMARY
; RUN: %python %S/../util/verify_ifss_loop_exit_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); assert r['exit_semantics']=='post-update-priority-equality-break-v2' and r['break_count']==2 and len(r['execution_table'])==512 and r['execution_table'][234]=={'trip':3,'break_at':[5,2],'break_taken':True,'winner':1,'executions':3} and r['execution_table'][210]['winner']==0"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['execution_table'][234]['winner']=0; open(r'%t.winner-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_exit_manifest.py %t.winner-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['breaks'][0]['condition_site']=r['breaks'][1]['condition_site']; open(r'%t.site-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_exit_manifest.py %t.site-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['states'][0]['break_liveout_sites'][1]=r['states'][0]['phi_site']; open(r'%t.liveout-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_exit_manifest.py %t.liveout-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['proof_fingerprint']='1'; open(r'%t.hash-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_exit_manifest.py %t.hash-tamper
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=0 %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.disabled.ll
; RUN: %filecheck %s --input-file=%t.disabled.ll --check-prefix=DISABLED
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,b,c: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,b,c]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.disabled.ll',a,b,c)==run(r'%t.lowered.ll',a,b,c) for a in range(8) for b in range(8) for c in range(8))"
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf '\000\007\007' | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['solver_queries'] >= 1 and d['solver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*')]; assert models and any(len(m)>=3 and (m[0]&7)>=3 and (m[1]&7)>2 and (m[2]&7)==2 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @multi_break_loop(
    i8 %raw_trip, i8 %raw_break0, i8 %raw_break1) {
entry:
  %trip = and i8 %raw_trip, 7
  %break_at0 = and i8 %raw_break0, 7
  %break_at1 = and i8 %raw_break1, 7
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %check1 ]
  %state = phi i8 [ 10, %entry ], [ %state_next, %check1 ]
  %continue = icmp ult i8 %index, %trip
  br i1 %continue, label %update, label %normal

update:
  %state_next = add i8 %state, 3
  %index_next = add i8 %index, 1
  br label %check0

check0:
  %should_break0 = icmp eq i8 %index, %break_at0
  br i1 %should_break0, label %break0, label %check1

check1:
  %should_break1 = icmp eq i8 %index, %break_at1
  br i1 %should_break1, label %break1, label %loop

normal:
  %normal_state = phi i8 [ %state, %loop ]
  br label %final

break0:
  %break_state0 = phi i8 [ %state_next, %check0 ]
  br label %final

break1:
  %break_state1 = phi i8 [ %state_next, %check1 ]
  br label %final

final:
  %state_out = phi i8 [ %normal_state, %normal ],
                      [ %break_state0, %break0 ],
                      [ %break_state1, %break1 ]
  %exit_bias = phi i8 [ 0, %normal ],
                      [ 100, %break0 ],
                      [ 200, %break1 ]
  %result = add i8 %state_out, %exit_bias
  ret i8 %result
}

define i32 @semantic_main() {
entry:
  %input = alloca [3 x i8], align 1
  %pointer = getelementptr [3 x i8], ptr %input, i64 0, i64 0
  %read = call i64 @read(i32 0, ptr %pointer, i64 3)
  %raw_trip = load i8, ptr %pointer, align 1
  %break0_ptr = getelementptr i8, ptr %pointer, i64 1
  %raw_break0 = load i8, ptr %break0_ptr, align 1
  %break1_ptr = getelementptr i8, ptr %pointer, i64 2
  %raw_break1 = load i8, ptr %break1_ptr, align 1
  %result = call i8 @multi_break_loop(
      i8 %raw_trip, i8 %raw_break0, i8 %raw_break1)
  %status = zext i8 %result to i32
  ret i32 %status
}

define i32 @main() {
entry:
  %input = alloca [3 x i8], align 1
  %pointer = getelementptr [3 x i8], ptr %input, i64 0, i64 0
  %read = call i64 @read(i32 0, ptr %pointer, i64 3)
  %raw_trip = load i8, ptr %pointer, align 1
  %break0_ptr = getelementptr i8, ptr %pointer, i64 1
  %raw_break0 = load i8, ptr %break0_ptr, align 1
  %break1_ptr = getelementptr i8, ptr %pointer, i64 2
  %raw_break1 = load i8, ptr %break1_ptr, align 1
  %result = call i8 @multi_break_loop(
      i8 %raw_trip, i8 %raw_break0, i8 %raw_break1)
  %target = icmp eq i8 %result, 219
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; SUMMARY-LABEL: define i8 @multi_break_loop
; SUMMARY-LABEL: entry:
; SUMMARY: %ifss.loop.break_better = icmp ult i8 %break_at0, %trip, !symcc.ifss_loop_break ![[BREAK:[0-9]+]]
; SUMMARY: %ifss.loop.break_at = select i1 %ifss.loop.break_better, i8 %break_at0, i8 %trip, !symcc.ifss_loop_break ![[BREAK]]
; SUMMARY: %ifss.loop.break_better{{[0-9]+}} = icmp ult i8 %break_at1, %ifss.loop.break_at, !symcc.ifss_loop_break ![[BREAK]]
; SUMMARY: %ifss.loop.break_winner{{[0-9]+}} = select i1 %ifss.loop.break_better{{[0-9]+}}, i8 1, i8 %ifss.loop.break_winner, !symcc.ifss_loop_break ![[BREAK]]
; SUMMARY: %ifss.loop.executions = select i1 %ifss.loop.break_taken, i8 %ifss.loop.break_iterations, i8 %trip, !symcc.ifss_loop_break ![[BREAK]]
; SUMMARY: br label %ifss.loop.break.dispatch, {{.*}}!symcc.ifss_loop_break ![[BREAK]]
; SUMMARY-LABEL: ifss.loop.break.dispatch:
; SUMMARY: br i1 %ifss.loop.break_selected, label %break0, label %ifss.loop.break.dispatch{{[0-9]+}}, !symcc.ifss_loop_break ![[BREAK]]
; SUMMARY-LABEL: ifss.loop.break.dispatch{{[0-9]+}}:
; SUMMARY: br i1 %ifss.loop.break_selected{{[0-9]+}}, label %break1, label %normal, !symcc.ifss_loop_break ![[BREAK]]
; SUMMARY-NOT: loop:
; SUMMARY-NOT: update:
; SUMMARY-NOT: check0:
; SUMMARY-NOT: check1:
; SUMMARY: ![[BREAK]] = !{!"bounded-multi-break-loop-exit-v2", i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 7, i32 1, i32 2

; DISABLED-LABEL: define i8 @multi_break_loop
; DISABLED: loop:
; DISABLED: update:
; DISABLED: check0:
; DISABLED: check1:
; DISABLED-NOT: bounded-multi-break-loop-exit-v2

; IFSS-LABEL: define i8 @multi_break_loop
; IFSS-NOT: loop:
; IFSS: call ptr @_sym_build_unsigned_less_than
; IFSS: call ptr @_sym_build_ite
; IFSS: !"bounded-multi-break-loop-exit-v2"
