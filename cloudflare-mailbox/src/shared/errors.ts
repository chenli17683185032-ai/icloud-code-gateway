export class AppError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "AppError";
  }
}

export class ValidationError extends AppError {
  constructor(message = "请求内容不正确。") {
    super("invalid_request", 422, message);
  }
}

export class UnauthorizedError extends AppError {
  constructor(message = "邮箱或 Token 不正确。") {
    super("unauthorized", 401, message);
  }
}

export class NotFoundError extends AppError {
  constructor(message = "记录不存在。") {
    super("not_found", 404, message);
  }
}

export class RateLimitError extends AppError {
  constructor() {
    super("rate_limited", 429, "尝试次数过多，请一分钟后再试。");
  }
}
