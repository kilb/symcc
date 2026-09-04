// RUN: rm -f %t.dist
// RUN: env SYMCC_COLOR_TARGETS=target SYMCC_COLORATION_OUT=%t.dist %symcc -g -O0 %s -o %t
// RUN: python3 -c "rows=[l for l in open(r'%t.dist') if l.strip() and not l.startswith('#')]; vals=[float(l.split()[1]) for l in rows]; assert 0.0 in vals and max(vals) >= 1.0 and any(' main ' in l and float(l.split()[1]) > 0 for l in rows)"

static int target(int x) {
  if (x == 11)
    return 1;
  return 0;
}

static int other(int x) {
  if (x == 13)
    return 2;
  return 0;
}

int main(int argc, char **argv) {
  (void)argv;
  int (*callee)(int) = argc > 2 ? other : target;
  if (argc > 1)
    return callee(argc);
  return 0;
}
