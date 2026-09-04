; RUN: %symcc -O0 %s -o %t
; RUN: echo -ne "\x00\x00\x00\x00" | %t 2>&1 | %filecheck %s

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() {
entry:
  %input = alloca i32, align 4
  %bytes = bitcast i32* %input to i8*
  %read = call i64 @read(i32 0, i8* %bytes, i64 4)
  %value = load i32, i32* %input, align 4

  ; Exercise both width-changing visitors directly. inttoptr must zero-extend
  ; from i32 to the 64-bit pointer width, and ptrtoint must then truncate.
  %pointer = inttoptr i32 %value to i8*
  %wide = ptrtoint i8* %pointer to i128
  %narrow = ptrtoint i8* %pointer to i16
  %wide_match = icmp eq i128 %wide, 305419896
  %narrow_match = icmp eq i16 %narrow, 22136
  %both = and i1 %wide_match, %narrow_match
  br i1 %both, label %yes, label %no

yes:
  ; SIMPLE: Found diverging input
  ; QSYM: New testcase
  ret i32 0

no:
  ; ANY-NOT: invalid extract
  ret i32 0
}

declare i64 @read(i32, i8*, i64)
