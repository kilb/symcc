// RUN: rm -f %t.dist %t.merged
// RUN: env SYMCC_COLOR_TARGETS=target SYMCC_COLORATION_OUT=%t.dist %symcc -g -O0 %s -x c %S/directed_coloration_multimodule_target.inc -o %t
// RUN: python3 %S/../util/merge_directed_distance.py %t.dist --output %t.merged
// RUN: python3 -c "rows=[l for l in open(r'%t.merged') if l.strip() and not l.startswith('#')]; assert any(' main ' in l and float(l.split()[1]) > 0 for l in rows); assert any(' target ' in l and float(l.split()[1]) == 0 for l in rows)"

extern int target(int);

int main(int argc, char **argv) {
  (void)argv;
  if (argc > 1)
    return target(argc);
  return 0;
}
