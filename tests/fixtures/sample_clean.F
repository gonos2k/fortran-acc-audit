! Fixture with VL=1 — should produce zero audit violations.
! Models the correct post-Phase-F pattern.

module sample_clean_module
  implicit none
  public :: driver, column_sub

contains

  subroutine driver(n, x)
    integer, intent(in) :: n
    real, intent(inout) :: x(n)
    integer :: i

    !$acc data copy(x)
    !$acc parallel loop gang vector_length(1) private(tmp)
    do i = 1, n
      block
        real :: tmp(10)
        call column_sub(x(i), tmp)
      end block
    end do
    !$acc end data
  end subroutine driver

  subroutine column_sub(a, b)
    !$acc routine seq
    real, intent(inout) :: a
    real, intent(out) :: b(10)
    integer :: k
    do k = 1, 10
      b(k) = a * real(k)
    end do
    a = sum(b)
  end subroutine column_sub

end module sample_clean_module
