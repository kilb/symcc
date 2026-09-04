; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='symbolic-region-cyclic-byte-composition'); w=c['backedge_state']['symbolic_region_writer']; assert r['analysis']=='llvm-memoryssa-aa-symbolic-region-cyclic-byte-lane-revalidated-v17' and s['state_schema']=='symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v17' and w['region_extent']=='8' and w['index_bits']==64 and [x['source_kind'] for x in c['backedge_state']['lanes']]==['carry']*4 and [[y['index_value'] for y in x['cases']] for x in w['lane_cases']]==[[0],[1],[2],[3]]"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); w=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='symbolic-region-cyclic-byte-composition')['backedge_state']['symbolic_region_writer']; w['region_extent']='9'; open(r'%t.extent-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.extent-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); w=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='symbolic-region-cyclic-byte-composition')['backedge_state']['symbolic_region_writer']; w['lane_cases'][2]['cases'][0]['source_byte']=1; open(r'%t.case-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.case-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='icmp eq i64 %index, 2'; new='icmp eq i64 %index, 7'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); values=(0,1,17,34,51,68,127,128,170,255); run=lambda p,a,n,v: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,n,v]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,n,v)==run(r'%t.lowered.ll',a,n,v) for a in (64,65,66) for n in range(9) for v in values)"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\\x01\\xaa',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m)>=3 and m[0]==65 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_loop(i8 %selector, i8 %iterations, i8 %value) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i32 1144201745, ptr %slot, align 4
  %limit = zext i8 %iterations to i64
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %done = icmp uge i64 %index, %limit
  br i1 %done, label %loop.exit, label %body

body:
  %element = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 %value, ptr %element, align 1
  %next = add i64 %index, 1
  br label %loop

loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  br label %continuation_a

deep:
  store i32 305419896, ptr %slot, align 4
  br label %continuation_b

continuation_a:
  %tag_a = phi i8 [ 1, %left ]
  %state = load i32, ptr %slot, align 4
  %b0 = trunc i32 %state to i8
  %s1 = lshr i32 %state, 8
  %b1 = trunc i32 %s1 to i8
  %s2 = lshr i32 %state, 16
  %b2 = trunc i32 %s2 to i8
  %s3 = lshr i32 %state, 24
  %b3 = trunc i32 %s3 to i8
  %x01 = xor i8 %b0, %b1
  %x23 = xor i8 %b2, %b3
  %fold = xor i8 %x01, %x23
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

declare noalias ptr @malloc(i64)
declare i64 @read(i32, ptr, i64)

define i32 @semantic_main() {
entry:
  %input = alloca [3 x i8], align 1
  %read = call i64 @read(i32 0, ptr %input, i64 3)
  %selector.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 1
  %value.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %value = load i8, ptr %value.ptr, align 1
  %choice = call i8 @choose_loop(i8 %selector, i8 %iterations, i8 %value)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [3 x i8], align 1
  %read = call i64 @read(i32 0, ptr %input, i64 3)
  %selector.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 1
  %value.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %value = load i8, ptr %value.ptr, align 1
  %choice = call i8 @choose_loop(i8 %selector, i8 %iterations, i8 %value)
  %target = icmp eq i8 %choice, 0
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

; LOWERED-LABEL: define i8 @choose_loop
; LOWERED-LABEL: loop:
; LOWERED: %ifss.cont.memory.cycle = phi i32
; LOWERED-LABEL: body:
; LOWERED: %ifss.cont.region.hit = icmp eq i64 %index, 0
; LOWERED: %ifss.cont.byte.region = select i1 %ifss.cont.region.hit, i8 %value
; LOWERED: %ifss.cont.region.hit{{[0-9]*}} = icmp eq i64 %index, 1
; LOWERED: %ifss.cont.region.hit{{[0-9]*}} = icmp eq i64 %index, 2
; LOWERED: %ifss.cont.region.hit{{[0-9]*}} = icmp eq i64 %index, 3
; LOWERED-LABEL: ifss.cont.dispatch:
; LOWERED: %ifss.cont.memory = phi i32
; LOWERED: symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v17

; IFSS-LABEL: define i8 @choose_loop
; IFSS: %ifss.cont.memory.cycle = phi i32
; IFSS: call ptr @_sym_build_equal
; IFSS: call ptr @_sym_build_ite
; IFSS: symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v17
