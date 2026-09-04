; A bounded native oracle for Hydra transformed-only failure filtering.
; Input "A" takes the valid load in the original program.  Input "B" takes
; the invalid load.  Aggressive control-flow melding executes both loads, so
; "A" becomes a transformed-only failure while "B" remains a real failure.

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

@safe = private global i8 7, align 1
@invalid_pointer = private global ptr inttoptr (i64 1 to ptr), align 8

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %byte = load i8, ptr %input, align 1
  %condition = icmp eq i8 %byte, 65
  br i1 %condition, label %valid, label %invalid, !symcc.site_id !0

valid:
  %valid_value = load i8, ptr @safe, align 1
  br label %merge

invalid:
  %bad = load ptr, ptr @invalid_pointer, align 8
  %invalid_value = load i8, ptr %bad, align 1
  br label %merge

merge:
  %result = phi i8 [ %valid_value, %valid ], [ %invalid_value, %invalid ]
  %consume = zext i8 %result to i32
  call void asm sideeffect "", "r"(i32 %consume)
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

!0 = !{i64 424248}
