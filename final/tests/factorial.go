package main

func Factorial(n uint32) (result uint32) {
	if (n > 0) {
		var result uint32 = n * Factorial(n-1)
		return result
	}
	return 1
}

func main() {
	var num1 uint32
	scanln(&num1)
	a := Factorial(num1)
	println(num1, a)
}
