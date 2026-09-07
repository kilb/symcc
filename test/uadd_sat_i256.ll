; RUN: %symcc -O0 %s -o %t
; RUN: printf '\377' | %t 2>&1 | %filecheck %s

@.done = private unnamed_addr constant [6 x i8] c"done\0A\00", align 1

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %value = load i8, i8* %input, align 1
  %wide = zext i8 %value to i256
  %sum = call i256 @llvm.uadd.sat.i256(i256 %wide, i256 -11)
  %saturated = icmp eq i256 %sum, -1
  br i1 %saturated, label %finish, label %finish

finish:
  ; SIMPLE: Trying to solve
  ; QSYM: SMT
  ; ANY: done
  %written = call i64 @write(i32 2, i8* getelementptr inbounds ([6 x i8], [6 x i8]* @.done, i64 0, i64 0), i64 5)
  ret i32 0
}

declare i64 @read(i32, i8* nocapture, i64)
declare i64 @write(i32, i8* nocapture readonly, i64)
declare i256 @llvm.uadd.sat.i256(i256, i256)
