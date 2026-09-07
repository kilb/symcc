; RUN: %symcc -O0 %s -o %t
; RUN: printf '\000' | %t 2>&1 | %filecheck %s

@.done = private unnamed_addr constant [6 x i8] c"done\0A\00", align 1

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %value = load i8, i8* %input, align 1

  ; Shift amounts are reduced modulo the element width: 9 mod 8 == 1.
  %left = call i8 @llvm.fshl.i8(i8 %value, i8 -128, i8 9)
  %left_ok = icmp eq i8 %left, 1
  br i1 %left_ok, label %check_right, label %finish

check_right:
  ; 17 mod 8 == 1; this checks both the modulo and high/low half extraction.
  %right = call i8 @llvm.fshr.i8(i8 1, i8 %value, i8 17)
  %right_ok = icmp eq i8 %right, -128
  br i1 %right_ok, label %finish, label %finish

finish:
  ; SIMPLE-COUNT-2: Trying to solve
  ; QSYM-COUNT-4: SMT
  ; ANY: done
  %written = call i64 @write(i32 2, i8* getelementptr inbounds ([6 x i8], [6 x i8]* @.done, i64 0, i64 0), i64 5)
  ret i32 0
}

declare i64 @read(i32, i8* nocapture, i64)
declare i64 @write(i32, i8* nocapture readonly, i64)
declare i8 @llvm.fshl.i8(i8, i8, i8)
declare i8 @llvm.fshr.i8(i8, i8, i8)
