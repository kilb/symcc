; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); s=r['memory_slots'][0]; x=next(x for x in s['exit_states'] if x['state_kind']=='symbolic-region-writer-graph'); w=next(y for y in x['writer_layers'] if y['kind']=='symbolic-region-write'); cs=w['lane_cases']; assert r['analysis']=='llvm-memoryssa-aa-symbolic-region-writer-graph-revalidated-v16' and s['state_schema']=='symbolic-region-writer-graph-continuation-memory-tuple-v16' and w['region_extent']=='8' and w['index_bits']==64 and w['base_offset']==0 and len(cs)==4 and [[(c['index_value'],c['source_byte']) for c in z['cases']] for z in cs]==[[(0,0),(-1,1)],[(1,0),(0,1)],[(2,0),(1,1)],[(3,0),(2,1)]]"
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); w=next(y for x in r['memory_slots'][0]['exit_states'] for y in x.get('writer_layers',[]) if y['kind']=='symbolic-region-write'); w['region_extent']='9'; open(r'%t.extent-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.extent-tamper
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); w=next(y for x in r['memory_slots'][0]['exit_states'] for y in x.get('writer_layers',[]) if y['kind']=='symbolic-region-write'); w['lane_cases'][1]['cases'][1]['index_value']=1; open(r'%t.case-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.case-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='icmp eq i64 %index, 0'; new='icmp eq i64 %index, 1'; assert p.count(old)>=1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,n: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,n]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,n)==run(r'%t.lowered.ll',a,n) for a in (64,65,66) for n in range(256))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'A\\x00',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m)>=2 and m[0]==65 and m[1]%7==2 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_symbolic_region(i8 %selector, i8 %raw_index) {
entry:
  %heap = call ptr @malloc(i64 8)
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i32 1144201745, ptr %heap, align 4
  %bounded = urem i8 %raw_index, 7
  %index = zext i8 %bounded to i64
  %dynamic = getelementptr inbounds i8, ptr %heap, i64 %index
  store i16 -24142, ptr %dynamic, align 1
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep

middle:
  store i32 287454020, ptr %heap, align 4
  br label %continuation_a

deep:
  store i32 1432778632, ptr %heap, align 4
  br label %continuation_b

continuation_a:
  %tag_a = phi i8 [ 1, %left ], [ 2, %middle ]
  %state = load i32, ptr %heap, align 4
  %byte0 = trunc i32 %state to i8
  %shift1 = lshr i32 %state, 8
  %byte1 = trunc i32 %shift1 to i8
  %shift2 = lshr i32 %state, 16
  %byte2 = trunc i32 %shift2 to i8
  %shift3 = lshr i32 %state, 24
  %byte3 = trunc i32 %shift3 to i8
  %xor01 = xor i8 %byte0, %byte1
  %xor23 = xor i8 %byte2, %byte3
  %fold = xor i8 %xor01, %xor23
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

define i32 @semantic_main() {
entry:
  %input = alloca [2 x i8], align 1
  %read = call i64 @read(i32 0, ptr %input, i64 2)
  %selector.ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 0
  %index.ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 1
  %selector = load i8, ptr %selector.ptr, align 1
  %index = load i8, ptr %index.ptr, align 1
  %choice = call i8 @choose_symbolic_region(i8 %selector, i8 %index)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [2 x i8], align 1
  %read = call i64 @read(i32 0, ptr %input, i64 2)
  %selector.ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 0
  %index.ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 1
  %selector = load i8, ptr %selector.ptr, align 1
  %index = load i8, ptr %index.ptr, align 1
  %choice = call i8 @choose_symbolic_region(i8 %selector, i8 %index)
  %target = icmp eq i8 %choice, 33
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare noalias ptr @malloc(i64)
declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_symbolic_region
; LOWERED: %ifss.cont.region.hit = icmp eq i64 %index, -1
; LOWERED: %ifss.cont.byte.region = select i1 %ifss.cont.region.hit
; LOWERED: %ifss.cont.memory = phi i32
; LOWERED: symbolic-region-writer-graph-continuation-memory-tuple-v16

; IFSS-LABEL: define i8 @choose_symbolic_region
; IFSS: %ifss.cont.region.hit = icmp eq i64 %index, -1
; IFSS: call ptr @_sym_build_equal
; IFSS: call ptr @_sym_build_ite
; IFSS: symbolic-region-writer-graph-continuation-memory-tuple-v16
