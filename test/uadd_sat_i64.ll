; RUN: %symcc -O2 %s -o %t
; RUN: printf '\377\377\377\377\377\377\377\377' | %t 2>&1 | %filecheck %s

%struct._IO_FILE = type { i32, i8*, i8*, i8*, i8*, i8*, i8*, i8*, i8*, i8*, i8*, i8*, %struct._IO_marker*, %struct._IO_FILE*, i32, i32, i64, i16, i8, [1 x i8], i8*, i64, i8*, i8*, i8*, i8*, i64, i32, [20 x i8] }
%struct._IO_marker = type { %struct._IO_marker*, %struct._IO_FILE*, i32 }

@stderr = external dso_local local_unnamed_addr global %struct._IO_FILE*, align 8
@.yes = private unnamed_addr constant [4 x i8] c"yes\00", align 1
@.no = private unnamed_addr constant [3 x i8] c"no\00", align 1
@.format = private unnamed_addr constant [4 x i8] c"%s\0A\00", align 1

define dso_local i32 @main() local_unnamed_addr {
entry:
  %x = alloca i64, align 8
  %bytes = bitcast i64* %x to i8*
  %read = call i64 @read(i32 0, i8* nonnull %bytes, i64 8)
  %value = load i64, i64* %x, align 8
  %sum = call i64 @llvm.uadd.sat.i64(i64 %value, i64 40)
  %saturated = icmp eq i64 %sum, -1
  %answer = select i1 %saturated, i8* getelementptr inbounds ([4 x i8], [4 x i8]* @.yes, i64 0, i64 0), i8* getelementptr inbounds ([3 x i8], [3 x i8]* @.no, i64 0, i64 0)
  ; SIMPLE: Trying to solve
  ; SIMPLE: Found diverging input
  ; ANY: yes
  %stream = load %struct._IO_FILE*, %struct._IO_FILE** @stderr, align 8
  %printed = call i32 (%struct._IO_FILE*, i8*, ...) @fprintf(%struct._IO_FILE* %stream, i8* getelementptr inbounds ([4 x i8], [4 x i8]* @.format, i64 0, i64 0), i8* %answer)
  ret i32 0
}

declare i64 @read(i32, i8* nocapture, i64)
declare i32 @fprintf(%struct._IO_FILE* nocapture, i8* nocapture readonly, ...)
declare i64 @llvm.uadd.sat.i64(i64, i64)
