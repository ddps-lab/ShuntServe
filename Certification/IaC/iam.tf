resource "aws_iam_role" "s3-role" {
  name = "${var.prefix}-shunt-cert-s3-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })
}

# Workers read the model weights from S3
resource "aws_iam_role_policy_attachment" "s3-role-policy-attachment" {
  role       = aws_iam_role.s3-role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
}

resource "aws_iam_instance_profile" "s3-instance-profile" {
  name = "${var.prefix}-shunt-cert-s3-instance-profile"
  role = aws_iam_role.s3-role.name
}
